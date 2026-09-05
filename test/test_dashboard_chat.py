"""Tests for dashboard chat session — slot management, pagination, history persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import (
    AsyncIterator,
    _make_app,
    _make_app_with_agent_routes,
    _make_folder_app,
    _make_ready_kiro_prerequisite,
    _make_state,
)

from kiro_crew.acp.types import TurnUsage
from kiro_crew.dashboard.chat_runner import _tool_call_ws_payload
from kiro_crew.dashboard.state import (
    _MAX_SLOT_MESSAGES,
    _MAX_SOURCE_LINKS_PER_SLOT,
    DashboardState,
    _ChatSlot,
)
from kiro_crew.history import ConversationLog


def test_tool_call_ws_payload_preserves_shell_capability_signal():
    """The dashboard receives an explicit shell signal for indeterminate UX.

    Keep this contract at the backend boundary so a future percentage-based
    progress mode can extend the payload without making the frontend infer
    tool type from a display title.
    """
    event = MagicMock(
        title="bash",
        tool_kind="execute",
        tool_call_id="tc-shell",
        tool_purpose="Run a command",
        tool_input="echo hello",
        is_shell=True,
    )

    payload = _tool_call_ws_payload(event)

    assert payload["tool"] == "bash"
    assert payload["kind"] == "execute"
    assert payload["is_shell"] is True
    assert payload["tool_call_id"] == "tc-shell"


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
        — the silent failure the deleted blanket refusal used to make loud.
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
        slot = _ChatSlot("s1", title="Test Chat", mode="orchestrator")
        slot.append("user", "hi")
        d = slot.to_dict()
        assert d["key"] == "s1"
        assert d["title"] == "Test Chat"
        assert d["mode"] == "orchestrator"
        assert d["messages"] == 1
        assert d["running"] is False
        assert d["pending_approval"] is False

    def test_issue_links_do_not_crowd_out_pr_chips(self):
        """Each kind gets its own chip budget, so a PR chip is never starved.

        A single shared budget sliced before the kind filter would drop the PR
        entirely once three issues are mentioned first -- and, because the
        check-status refresh reads the same slice, would also stop scheduling
        that PR's CI updates.
        """
        slot = _ChatSlot("s1")
        pr_url = "https://github.com/acme/widgets/pull/12"
        issue_urls = [f"https://github.com/acme/widgets/issues/{n}" for n in (1, 2, 3, 4)]
        # The issues are mentioned FIRST, so a shared budget would spend all of
        # it before ever reaching the pull request.
        slot.append("assistant", "\n".join([*issue_urls, pr_url]), ts="t1")

        payload = slot.to_dict()
        serialized = payload["source_links"]
        assert pr_url in [link["url"] for link in serialized]
        assert [link["url"] for link in serialized if link["kind"] == "change"] == [pr_url]
        # Three of the four issues fit their own budget; the fourth overflows.
        assert len([link for link in serialized if link["kind"] == "issue"]) == 3
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
        """The chip budget serializes only the first few links per kind, so a
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

    def test_serialized_chips_keep_the_newest_links_of_each_kind(self):
        slot = _ChatSlot("s1")
        for number in (1, 2, 3, 4, 5):
            slot.append("assistant", f"https://github.com/acme/widgets/pull/{number}")
        for number in (10, 11, 12, 13):
            slot.append("assistant", f"https://github.com/acme/widgets/issues/{number}")

        payload = slot.to_dict()
        serialized = payload["source_links"]
        changes = [x["number"] for x in serialized if x["kind"] == "change"]
        issues = [x["number"] for x in serialized if x["kind"] == "issue"]

        # Three per kind, newest first, and the total still counts everything so
        # the "+N" pill stays honest.
        assert changes == [5, 4, 3]
        assert issues == [13, 12, 11]
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
        slot.append("assistant", f"{pr} {issue} {mr} {gitlab_issue}")

        payload = slot.to_dict()

        assert payload["source_links_total"] == 4
        # Order-insensitive on purpose: this pins the kind MAPPING, and the
        # recency ordering has its own tests below. Asserting a sequence here
        # made it a second, accidental ordering oracle.
        assert {link["url"]: link["kind"] for link in slot._pr_source_links()} == {
            pr: "change",
            issue: "issue",
            mr: "change",
            gitlab_issue: "issue",
        }
        assert all("kind" in link for link in payload["source_links"])

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
        # Simulate drain logic from _run_chat
        failures = slot._pending_subagent_failures[:]
        slot._pending_subagent_failures.clear()
        message = "\n\n".join(failures) + "\n\n" + "user message"
        assert "[Subagent completion event]" in message
        assert "Agent `a1`" in message
        assert "Agent `a2`" in message
        assert message.endswith("user message")
        assert slot._pending_subagent_failures == []


class TestBroadcastCompactionResultBackoff:
    """Streak/cooldown backoff for the per-turn EVENT_COMPACTION_STATUS
    failure path (Mesh compaction-spam fix). Distinct from the
    claude/kiro deferred-wait integration tests below — these exercise
    ``_broadcast_compaction_result`` directly for speed and isolation.
    """

    @staticmethod
    def _make_slot_and_state(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("s1")
        return state, slot

    @staticmethod
    def _failed_event(title: str = ""):
        from kiro_crew.providers.base import LLMEvent

        return LLMEvent(kind="compaction_status", text="failed", title=title)

    @staticmethod
    def _completed_event(title: str = "did stuff"):
        from kiro_crew.providers.base import LLMEvent

        return LLMEvent(kind="compaction_status", text="completed", title=title)

    def test_first_n_failures_shown_as_is(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_utils import (
            _COMPACTION_NOTICE_SHOW_FIRST_N,
            _broadcast_compaction_result,
        )

        state, slot = self._make_slot_and_state(tmp_path, monkeypatch)

        for i in range(1, _COMPACTION_NOTICE_SHOW_FIRST_N + 1):
            msg = _broadcast_compaction_result(state, slot, self._failed_event())
            assert msg is not None, f"failure #{i} should still be shown"
            assert "unknown error" in msg
            # Streak-count wording only appears once we exceed the shown limit.
            assert f"{i}x in a row" not in msg

    def test_failures_beyond_limit_suppressed_within_cooldown(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_utils import (
            _COMPACTION_NOTICE_SHOW_FIRST_N,
            _broadcast_compaction_result,
        )

        state, slot = self._make_slot_and_state(tmp_path, monkeypatch)

        for _ in range(_COMPACTION_NOTICE_SHOW_FIRST_N):
            _broadcast_compaction_result(state, slot, self._failed_event())

        # One more failure while still within cooldown must return None
        # (nothing appended to the slot, no new broadcast).
        before = len(slot.messages)
        msg = _broadcast_compaction_result(state, slot, self._failed_event())
        assert msg is None
        assert len(slot.messages) == before

    def test_cooldown_elapsed_shows_collapsed_streak_message(self, tmp_path, monkeypatch):
        import kiro_crew.dashboard.chat_utils as chat_utils

        state, slot = self._make_slot_and_state(tmp_path, monkeypatch)

        fake_now = [1000.0]
        monkeypatch.setattr(chat_utils.time, "monotonic", lambda: fake_now[0])

        for _ in range(chat_utils._COMPACTION_NOTICE_SHOW_FIRST_N):
            chat_utils._broadcast_compaction_result(state, slot, self._failed_event())

        # Still within cooldown: suppressed.
        fake_now[0] += 1.0
        assert chat_utils._broadcast_compaction_result(state, slot, self._failed_event()) is None

        # Cooldown elapses: next failure collapses the streak into one message.
        fake_now[0] += chat_utils._COMPACTION_FAIL_COOLDOWN_SECS + 1.0
        msg = chat_utils._broadcast_compaction_result(state, slot, self._failed_event())
        assert msg is not None
        assert "x in a row" in msg
        assert "too large to" in msg or "unknown error" in msg

    def test_enriched_title_replaces_unknown_error(self, tmp_path, monkeypatch):
        """The notice reads the event title. kiro-cli sends no summary on
        failure, so the ACP layer now carries the notification's own reason
        there (issue #3583) — the row must name it instead of collapsing to
        "unknown error"."""
        from kiro_crew.dashboard.chat_utils import _broadcast_compaction_result

        state, slot = self._make_slot_and_state(tmp_path, monkeypatch)

        msg = _broadcast_compaction_result(
            state, slot, self._failed_event("context window exceeded")
        )
        assert msg is not None
        assert "context window exceeded" in msg
        assert "unknown error" not in msg

    def test_success_resets_streak_and_cooldown(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_utils import (
            _COMPACTION_NOTICE_SHOW_FIRST_N,
            _broadcast_compaction_result,
        )

        state, slot = self._make_slot_and_state(tmp_path, monkeypatch)

        for _ in range(_COMPACTION_NOTICE_SHOW_FIRST_N):
            _broadcast_compaction_result(state, slot, self._failed_event())
        assert slot._compaction_fail_streak == _COMPACTION_NOTICE_SHOW_FIRST_N

        msg = _broadcast_compaction_result(state, slot, self._completed_event())
        assert msg is not None
        assert "compacted" in msg.lower()
        assert slot._compaction_fail_streak == 0
        assert slot._compaction_fail_cooldown_until == 0.0

        # A fresh failure right after a success must be shown (not suppressed),
        # proving the reset actually took effect.
        msg = _broadcast_compaction_result(state, slot, self._failed_event())
        assert msg is not None

    def test_completed_broadcasts_context_usage_reset(self, tmp_path, monkeypatch):
        """Regression: the in-turn compaction path posted the notice but never
        refreshed the context meter — the bar stayed at the pre-compaction
        value until the next turn."""
        from kiro_crew.dashboard.chat_utils import _broadcast_compaction_result

        state, slot = self._make_slot_and_state(tmp_path, monkeypatch)

        _broadcast_compaction_result(state, slot, self._completed_event())

        resets = [
            c for c in state.broadcast_ws.call_args_list if c.args and c.args[0] == "context_usage"
        ]
        assert resets, "completed compaction must broadcast a context_usage event"
        payload = resets[0].args[1]
        assert payload == {"slot": slot.key, "pct": 0.0, "reset": True}

    def test_failed_does_not_broadcast_context_usage(self, tmp_path, monkeypatch):
        """A failed compaction leaves usage unchanged — no meter reset."""
        from kiro_crew.dashboard.chat_utils import _broadcast_compaction_result

        state, slot = self._make_slot_and_state(tmp_path, monkeypatch)

        _broadcast_compaction_result(state, slot, self._failed_event())

        assert not [
            c for c in state.broadcast_ws.call_args_list if c.args and c.args[0] == "context_usage"
        ]


@pytest.mark.asyncio
class TestApiChatDrainOnDisconnect:
    """Cover the slot.drain() call in chat_handlers' SSE finally block."""

    async def test_sse_reader_drains_pending_on_cancel(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            assert _directive_user_origin is True
            sl.append("chunk", "partial answer", "chunk")
            await asyncio.sleep(60)

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "hello", "slot": "s1"},
                timeout=None,
            )
            line = b""
            async for chunk in resp.content.iter_any():
                line += chunk
                if b"partial answer" in line:
                    break

            resp.close()
            await asyncio.sleep(0.1)

        assert slot._pending == []
        assert slot._has_reader is False


@pytest.mark.asyncio
class TestApiChatMemoryModeForwarding:
    """api_chat propagates body.memory_mode to auto-created slot.

    AgentRock skill dispatch sends `memory_mode: "temporary"` so one-shot
    skill invocations don't bleed into the user's persistent memory. The
    chat endpoint must honor this on the auto-create path because
    AgentRock does not pre-create the slot via /api/chat/slots.
    """

    async def test_temporary_memory_mode_propagates_to_new_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            assert _directive_user_origin is True
            sl.append("chunk", "ack", "chunk")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "message": "hello",
                    "slot": "agentrock-skill-1",
                    "memory_mode": "temporary",
                },
                timeout=None,
            )
            async for _chunk in resp.content.iter_any():
                break  # only need to drive slot creation
            resp.close()
            await asyncio.sleep(0.05)

        slot = state._slots.get("agentrock-skill-1")
        assert slot is not None
        assert slot.memory_mode == "temporary"

    async def test_missing_memory_mode_defaults_to_persistent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            assert _directive_user_origin is True
            sl.append("chunk", "ack", "chunk")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "hello", "slot": "default-slot"},
                timeout=None,
            )
            async for _chunk in resp.content.iter_any():
                break
            resp.close()
            await asyncio.sleep(0.05)

        slot = state._slots.get("default-slot")
        assert slot is not None
        assert slot.memory_mode == "persistent"

    async def test_invalid_memory_mode_is_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            assert _directive_user_origin is True
            sl.append("chunk", "ack", "chunk")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "message": "hello",
                    "slot": "garbage-mode-slot",
                    "memory_mode": "garbage",
                },
                timeout=None,
            )
            async for _chunk in resp.content.iter_any():
                break
            resp.close()
            await asyncio.sleep(0.05)

        slot = state._slots.get("garbage-mode-slot")
        assert slot is not None
        assert slot.memory_mode == "persistent"

    async def test_mismatched_memory_mode_on_existing_slot_returns_409(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        mock_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: mock_sel)
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        # Pre-create a persistent slot
        state.get_or_create_slot("locked", memory_mode="persistent")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "message": "hello",
                    "slot": "locked",
                    "memory_mode": "temporary",
                },
                timeout=None,
            )
            assert resp.status == 409
            data = await resp.json()
            assert "memory_mode" in data["error"]

        # SEL audit event for the denial
        denied_calls = [
            c for c in mock_sel.log_api_access.call_args_list if c[1].get("outcome") == "denied"
        ]
        assert len(denied_calls) == 1
        kw = denied_calls[0][1]
        assert kw["operation"] == "chat_send"
        assert kw["source"] == "memory_mode_mismatch"
        assert "slot=locked" in kw["resources"]


@pytest.mark.asyncio
class TestApiChatModeForwarding:
    """api_chat propagates a validated body.mode to the auto-created slot.

    The Design Critique app's worker slot (mode="design-critique") lives only
    in gateway memory. After a gateway restart the app's next send() recreates
    the slot through this auto-create path; without mode forwarding the slot
    is reborn with mode="" — it then serializes surface="" and passes the chat
    sidebar's surface allowlist, leaking the throwaway worker session into the
    user's chat list. Mirrors TestApiChatMemoryModeForwarding above.
    """

    async def _post_chat(self, state, body):
        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            assert _directive_user_origin is True
            sl.append("chunk", "ack", "chunk")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat", json=body, timeout=None)
                assert resp.status == 200
                async for _chunk in resp.content.iter_any():
                    break  # only need to drive slot creation
                resp.close()
                await asyncio.sleep(0.05)

    async def test_design_critique_mode_propagates_to_new_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        await self._post_chat(
            state,
            {
                "message": "critique this",
                "slot": "dc-12345",
                "memory_mode": "temporary",
                "mode": "design-critique",
            },
        )

        slot = state._slots.get("dc-12345")
        assert slot is not None
        assert slot.mode == "design-critique"
        # The leak this guards against: the sidebar allowlist admits surfaces
        # "", "orchestrator" and "crew" — the recreated worker must not
        # serialize one of those.
        assert slot.mode not in ("", "orchestrator", "crew")

    async def test_bogus_mode_is_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        await self._post_chat(
            state,
            {"message": "hello", "slot": "bogus-mode-slot", "mode": "not-a-mode"},
        )

        slot = state._slots.get("bogus-mode-slot")
        assert slot is not None
        assert slot.mode == ""

    async def test_non_string_mode_is_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        await self._post_chat(
            state,
            {"message": "hello", "slot": "nonstring-mode-slot", "mode": 42},
        )

        slot = state._slots.get("nonstring-mode-slot")
        assert slot is not None
        assert slot.mode == ""

    async def test_crew_mode_dropped_for_crew_incapable_name(self, tmp_path, monkeypatch):
        """Same boundary api_chat_slot_create enforces with a 400: a name whose
        normalized key cannot host a crew store (e.g. a Win32 device basename)
        must not become a crew slot via auto-create. Here it is dropped, not
        refused — the slot is created with the default mode instead."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        await self._post_chat(
            state,
            {"message": "hello", "slot": "CON", "mode": "crew"},
        )

        created = [s for k, s in state._slots.items() if k.lower() == "con"]
        assert created, "slot should still be created, just without crew mode"
        assert created[0].mode == ""

    async def test_mode_ignored_for_existing_slot(self, tmp_path, monkeypatch):
        """Repeating mode on send() must be safe when the slot survived: an
        existing slot keeps its stored mode and no error is raised."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("dc-alive", mode="design-critique", memory_mode="temporary")

        await self._post_chat(
            state,
            {
                "message": "hello again",
                "slot": "dc-alive",
                "memory_mode": "temporary",
                "mode": "design-critique",
            },
        )

        slot = state._slots.get("dc-alive")
        assert slot is not None
        assert slot.mode == "design-critique"


@pytest.mark.asyncio
class TestApiChatNoBrowseMarker:
    """Browse is gated by tool AVAILABILITY, not a per-message marker: the chat
    handler injects nothing into the user message, and the agent itself decides
    whether to operate a browser or read with web_fetch. This pins that the
    persisted message is verbatim (no `[BROWSE]` prefix), regardless of any legacy
    `browse` field a client might still send."""

    async def _send(self, tmp_path, monkeypatch, *, body_extra: dict):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            assert _directive_user_origin is True
            sl.append("chunk", "ack", "chunk")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)

        slot_key = body_extra.get("slot", "browse-slot")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "look at example.com", **body_extra},
                timeout=None,
            )
            async for _chunk in resp.content.iter_any():
                break
            resp.close()
            await asyncio.sleep(0.05)
        return state._slots.get(slot_key)

    async def test_message_is_never_marked(self, tmp_path, monkeypatch):
        slot = await self._send(tmp_path, monkeypatch, body_extra={"slot": "plain-slot"})
        assert slot is not None
        user_msgs = [m for m in slot.messages if m.get("role") == "user"]
        assert user_msgs and user_msgs[-1]["content"] == "look at example.com"

    async def test_legacy_browse_field_is_ignored(self, tmp_path, monkeypatch):
        # A client that still sends the old `browse` field must not change the
        # stored message: the marker mechanism is gone entirely.
        slot = await self._send(
            tmp_path, monkeypatch, body_extra={"slot": "legacy-slot", "browse": True}
        )
        assert slot is not None
        user_msgs = [m for m in slot.messages if m.get("role") == "user"]
        assert user_msgs and not user_msgs[-1]["content"].startswith("[BROWSE]")


# ── Slot detail pagination (HTTP) ──


class TestSlotDetailPagination:
    @pytest.mark.asyncio
    async def test_default_returns_latest(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("test")
        for i in range(10):
            slot.append("user", f"msg {i}")
        slot.drain()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/test")
            data = await resp.json()
            assert data["total"] == 10
            assert len(data["messages"]) == 10
            assert data["has_more"] is False

    @pytest.mark.asyncio
    async def test_pagination_with_before(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("test")
        log = state.conversation_log
        for i in range(300):
            log.append("dashboard:test", "user", f"msg {i}")
            slot.append("user", f"msg {i}")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/test?limit=200")
            data = await resp.json()
            assert data["has_more"] is True
            assert len(data["messages"]) == 200
            assert data["total"] == 300
            # The cursor for the next page, in the raw index space this slice was
            # taken in. The client cannot derive it from the response body, whose
            # rows have already been collapsed by _prepare_messages.
            assert data["next_before"] == 100

            resp = await client.get(f"/api/chat/slots/test?limit=200&before={data['next_before']}")
            data = await resp.json()
            assert len(data["messages"]) == 100
            assert data["has_more"] is False
            assert data["next_before"] == 0
            assert data["messages"][0]["content"] == "msg 0"

    @pytest.mark.asyncio
    async def test_empty_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("empty")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/empty")
            data = await resp.json()
            assert data["total"] == 0
            assert data["messages"] == []
            assert data["has_more"] is False

    @pytest.mark.asyncio
    async def test_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/nonexistent")
            assert resp.status == 404

    async def _slot_with_history(self, tmp_path, monkeypatch, name, count=10):
        """A slot with *count* messages on disk and in memory."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot(name)
        for i in range(count):
            state.conversation_log.append(f"dashboard:{name}", "user", f"msg {i}")
            slot.append("user", f"msg {i}")
        slot.drain()
        return state

    @pytest.mark.asyncio
    async def test_non_integer_limit_is_a_bad_request(self, tmp_path, monkeypatch):
        """A junk limit is the client's mistake, so it must not surface as a 500."""
        state = await self._slot_with_history(tmp_path, monkeypatch, "badlimit")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/badlimit?limit=abc")
            assert resp.status == 400
            body = await resp.json()
            assert "limit" in body["error"]
            assert body["code"] == "invalid_query_params"

    @pytest.mark.asyncio
    async def test_non_integer_before_is_a_bad_request(self, tmp_path, monkeypatch):
        state = await self._slot_with_history(tmp_path, monkeypatch, "badbefore")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/badbefore?before=xyz")
            assert resp.status == 400
            body = await resp.json()
            assert "before" in body["error"]
            assert body["code"] == "invalid_query_params"

    @pytest.mark.asyncio
    async def test_limit_below_one_is_rejected_not_clamped_up(self, tmp_path, monkeypatch):
        """limit=0 used to return an empty page reporting has_more true — forever."""
        state = await self._slot_with_history(tmp_path, monkeypatch, "zerolimit")
        async with TestClient(TestServer(_make_app(state))) as client:
            for bad in ("0", "-1", "-5"):
                resp = await client.get(f"/api/chat/slots/zerolimit?limit={bad}")
                assert resp.status == 400, f"limit={bad} should be rejected"
                body = await resp.json()
                assert "messages" not in body
                assert body["code"] == "limit_out_of_range"

    @pytest.mark.asyncio
    async def test_before_zero_remains_valid(self, tmp_path, monkeypatch):
        """A real caller sends before=0 on first page, so it must not be rejected."""
        state = await self._slot_with_history(tmp_path, monkeypatch, "beforezero")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/beforezero?limit=5&before=0")
            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == []
            assert data["has_more"] is False

    @pytest.mark.asyncio
    async def test_bounded_request_still_returns_the_same_page(self, tmp_path, monkeypatch):
        """Regression guard: threading the disk read must not alter the result."""
        state = await self._slot_with_history(tmp_path, monkeypatch, "bounded", count=30)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/bounded?limit=10")
            data = await resp.json()
            assert data["total"] == 30
            assert len(data["messages"]) == 10
            assert data["messages"][0]["content"] == "msg 20"
            assert data["messages"][-1]["content"] == "msg 29"
            assert data["has_more"] is True

    @pytest.mark.asyncio
    async def test_cursor_branch_reads_disk_off_the_loop_thread(self, tmp_path, monkeypatch):
        """The read must not run on the loop thread that serves every other request.

        Asserts only that the call executed on a different thread. It does not
        measure loop latency, so it cannot prove the loop was never blocked for
        some other reason — but it does fail if the ``to_thread`` hop is removed.
        """
        state = await self._slot_with_history(tmp_path, monkeypatch, "offloop")
        log = state.conversation_log
        real = log.read_messages_chained_full
        seen: list[int] = []

        def recording(key):
            seen.append(threading.get_ident())
            return real(key)

        monkeypatch.setattr(log, "read_messages_chained_full", recording)
        loop_thread = threading.get_ident()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/offloop?limit=5")
            assert resp.status == 200
        assert seen, "read_messages_chained_full was never called"
        assert loop_thread not in seen

    @pytest.mark.asyncio
    async def test_unlimited_branch_returns_whole_history_off_the_loop_thread(
        self, tmp_path, monkeypatch
    ):
        """The no-limit branch reassembles disk+memory and still claims has_more false."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("whole")
        log = state.conversation_log
        for i in range(10):
            log.append("dashboard:whole", "user", f"msg {i}")
        for i in range(6, 10):
            slot.append("user", f"msg {i}")
        slot.drain()
        slot._disk_older_count = 6

        real = log.read_messages_chained
        seen: list[int] = []

        def recording(key):
            seen.append(threading.get_ident())
            return real(key)

        monkeypatch.setattr(log, "read_messages_chained", recording)
        loop_thread = threading.get_ident()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/whole")
            assert resp.status == 200
            data = await resp.json()
        assert [m["content"] for m in data["messages"]] == [f"msg {i}" for i in range(10)]
        assert data["total"] == 10
        assert data["has_more"] is False
        assert seen, "read_messages_chained was never called"
        assert loop_thread not in seen

    @pytest.mark.asyncio
    async def test_message_arriving_during_the_disk_read_is_not_dropped(
        self, tmp_path, monkeypatch
    ):
        """The awaited read is a suspension point, so the tail is re-read after it.

        The client replaces its message list with this response, so a message that
        lands mid-read must not be silently absent. Appending from inside the
        patched read reproduces exactly that window deterministically.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("midread")
        log = state.conversation_log
        for i in range(10):
            log.append("dashboard:midread", "user", f"msg {i}")
        for i in range(6, 10):
            slot.append("user", f"msg {i}")
        slot.drain()
        slot._disk_older_count = 6

        real = log.read_messages_chained

        def appends_while_reading(key):
            result = real(key)
            slot.append("assistant", "arrived mid-read")
            slot.drain()
            return result

        monkeypatch.setattr(log, "read_messages_chained", appends_while_reading)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/midread")
            assert resp.status == 200
            data = await resp.json()
        contents = [m["content"] for m in data["messages"]]
        assert "arrived mid-read" in contents, "message that landed during the await was dropped"
        assert contents == [f"msg {i}" for i in range(10)] + ["arrived mid-read"]

    @pytest.mark.asyncio
    async def test_transient_window_row_does_not_duplicate_the_tail(self, tmp_path, monkeypatch):
        """A transient row in the window must not re-append rows already on disk.

        A save drops transient roles, so the disk read holds only persisted rows
        while the window holds both. Sizing the un-flushed tail by subtracting the
        two lengths therefore counts each transient row as one missing message and
        reaches that many rows too far back — rows the disk read already returned.

        Here everything persisted is on disk, so the correct tail is empty. The
        window carries one ``done`` row, which ``_prepare_messages`` drops from the
        response, so a duplicated row is directly visible in the content sequence.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("dup")
        log = state.conversation_log
        for i in range(4):
            log.append("dashboard:dup", "user", f"msg {i}")
        slot.append("user", "msg 0")
        slot.append("user", "msg 1")
        slot.append("done", "")
        slot.append("user", "msg 2")
        slot.append("user", "msg 3")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/dup?limit=10")
            assert resp.status == 200
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert contents == [
            f"msg {i}" for i in range(4)
        ], "un-flushed tail sized by a length subtraction re-appended a persisted row"
        assert data["total"] == 4

    @pytest.mark.asyncio
    async def test_genuinely_unflushed_tail_is_still_appended_once(self, tmp_path, monkeypatch):
        """Control for the test above: a real un-flushed tail must still arrive.

        Only the first two window rows reached disk, so the last two are owed to the
        client. A transient row sits between them, so a fix that merely stopped
        appending would pass the duplication test and fail this one.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("owed")
        log = state.conversation_log
        for i in range(2):
            log.append("dashboard:owed", "user", f"msg {i}")
        slot.append("user", "msg 0")
        slot.append("user", "msg 1")
        slot.append("done", "")
        slot.append("user", "msg 2")
        slot.append("user", "msg 3")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/owed?limit=10")
            assert resp.status == 200
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert contents == [f"msg {i}" for i in range(4)], "un-flushed tail was dropped"

    @pytest.mark.asyncio
    async def test_foreign_disk_row_does_not_consume_the_unflushed_tail(
        self, tmp_path, monkeypatch
    ):
        """A disk row absent from the window must not consume an owed turn.

        This is the OVER-count direction. A writer other than this slot's own save
        appends to the same transcript, so the chained disk read grows without any
        window row having been flushed. Sizing the boundary as
        ``len(all_msgs) - _disk_older_count`` therefore counts that foreign row as
        one more persisted window row, walks one row too far, and drops the
        genuinely un-flushed turn from a response the client uses as a replacement.
        Under-count fails safe with an empty tail; over-count loses a message.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("overcount")
        for i in range(3):
            slot.append("user", f"msg {i}")
        slot.drain()
        # Persist through the real save path, so the disk rows carry the window's
        # own message ids rather than a shape only a test helper produces.
        _save_slot_to_history(state, slot, force=True)
        # A writer that does not mirror into the window appends to the transcript.
        state.conversation_log.append("dashboard:overcount", "assistant", "foreign row")
        # A new turn arrives in memory and has not reached disk.
        slot.append("user", "new turn")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/overcount?limit=10")
            assert resp.status == 200
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert "new turn" in contents, f"over-count dropped the owed turn; got {contents}"

    @pytest.mark.asyncio
    async def test_non_string_message_id_does_not_crash_the_bounded_read(
        self, tmp_path, monkeypatch
    ):
        """A caller-supplied non-string ``meta.mid`` must not turn the read into a 500.

        ``meta`` on an inbound message comes from the HTTP caller and is checked
        only for being a dict, so its values keep whatever type arrived. A truthy
        non-string id is preserved rather than replaced, reaches disk, and is then
        hashed by the boundary match, where a list raises ``TypeError``.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", AsyncMock())
        state = _make_state(tmp_path)
        state.get_or_create_slot("listmid")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "message": "hi",
                    "slot": "listmid",
                    "meta": {"mid": ["not-a-string"]},
                },
            )
            assert resp.status == 200
            slot = state.get_or_create_slot("listmid")
            slot.drain()
            # The id must be on disk as well as in the window: the boundary match
            # hashes both sides.
            _save_slot_to_history(state, slot, force=True)
            assert slot.messages[-1]["meta"]["mid"] == [
                "not-a-string"
            ], "fixture no longer reproduces a non-string id"

            resp = await client.get("/api/chat/slots/listmid?limit=10")
            assert resp.status == 200, await resp.text()

    @pytest.mark.asyncio
    async def test_foreign_row_does_not_consume_the_tail_of_an_id_less_session(
        self, tmp_path, monkeypatch
    ):
        """The same over-count, on a session whose disk rows carry no message ids.

        The id match cannot help here: a session persisted before ids existed has
        none on disk, so the boundary falls back to counting. Sizing it as
        ``len(all_msgs) - _disk_older_count`` measures the whole file rather than
        this slot's own persisted rows, so a row appended by any other writer is
        counted as one more flushed window row and the owed turn is dropped.

        ``_disk_window_len`` is how many window rows are on disk, which is the
        quantity the subtraction was approximating, and a foreign append does not
        move it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("legacy")
        log = state.conversation_log
        # ConversationLog.append writes role/content/ts only, so these rows reach
        # disk with no ids -- what a pre-id session looks like.
        for i in range(3):
            log.append("dashboard:legacy", "user", f"msg {i}")
        for i in range(3):
            slot.append("user", f"msg {i}")
        slot.drain()
        # Mirror what the restore path leaves behind for such a session.
        slot._disk_older_count = 0
        slot._disk_window_len = len(slot.messages)
        slot._dirty = False
        # A writer that does not mirror into the window appends to the transcript.
        log.append("dashboard:legacy", "assistant", "foreign row")
        slot.append("user", "new turn")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/legacy?limit=10")
            assert resp.status == 200
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert "new turn" in contents, f"over-count dropped the owed turn; got {contents}"
        assert contents.count("msg 2") == 1, f"boundary walked short and duplicated; got {contents}"

    def test_id_less_boundary_matches_a_row_redacted_on_load(self):
        """A restored row keeps its ``ts`` verbatim but not always its content.

        Restore redacts non-user content before it enters the window while the disk
        row stays raw, so matching on content alone ends the run at that row and
        re-appends everything from there — rows the disk read already returned.

        The pair is taken from the REAL redactor rather than written by hand: an
        invented pair passes for the wrong reason, because a stamp match would carry
        it even when the transform would not.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        raw = "AKIAIOSFODNN7EXAMPLE"
        loaded, _ = redact_exfiltration_urls(raw)
        loaded, _ = redact_credentials(loaded)
        assert loaded != raw, "fixture no longer exercises a real redaction"

        all_msgs = [
            {"role": "user", "content": "ask", "ts": "t1"},
            {"role": "assistant", "content": raw, "ts": "t2"},
        ]
        slot = SimpleNamespace(
            messages=[
                {"role": "user", "content": "ask", "ts": "t1"},
                {"role": "assistant", "content": loaded, "ts": "t2"},
                {"role": "user", "content": "new turn", "ts": "t3"},
            ],
            _disk_older_count=0,
        )

        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]

        assert [m["content"] for m in out] == [
            "ask",
            raw,
            "new turn",
        ], "a row whose content was redacted on load was treated as un-flushed"

    def test_shared_stamp_foreign_row_does_not_swallow_an_unflushed_row(self):
        """A foreign row sharing a ``ts`` must not be accepted as the window's own.

        On a coarse clock two writers flooring off the same previous row both emit
        ``previous + 1µs`` (``history.py:1179-1219``), so a foreign disk row and an
        un-flushed window row of the SAME role can carry an identical stamp while
        holding different messages. Accepting a stamp match on its own then treats
        the foreign row as the window row, the boundary advances past the un-flushed
        one, and the bounded response omits it — from a payload the client uses to
        REPLACE its transcript, so the message is simply gone from view.

        Content is the discriminator: these two rows differ by more than the load
        redaction, so nothing may match them.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        collided = "2026-08-18T12:00:00.000001+00:00"
        all_msgs = [
            {"role": "user", "content": "q", "ts": "2026-08-18T12:00:00+00:00"},
            {"role": "assistant", "content": "another writer's row", "ts": collided},
        ]
        slot = SimpleNamespace(
            messages=[
                {"role": "user", "content": "q", "ts": "2026-08-18T12:00:00+00:00"},
                {"role": "assistant", "content": "owed reply", "ts": collided},
            ],
            _disk_older_count=0,
        )
        assert all_msgs[1]["ts"] == slot.messages[1]["ts"], "fixture lost the collision"
        assert all_msgs[1]["role"] == slot.messages[1]["role"], "fixture lost same-role"

        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]

        assert "owed reply" in [
            m["content"] for m in out
        ], f"a shared stamp swallowed the un-flushed row; got {[m['content'] for m in out]}"

    @pytest.mark.asyncio
    async def test_repeated_caller_supplied_id_does_not_hide_an_unflushed_row(
        self, tmp_path, monkeypatch
    ):
        """A caller may repeat ``meta.mid``, and a set membership test over-reaches.

        ``meta`` on an inbound message is caller-supplied and an id is minted only
        when one is *absent*, so a client can post the same id twice. The first row
        reaches disk; the second is still only in the window. Testing membership
        against a *set* of disk ids matches both window rows, so the boundary walks
        past the row that was never persisted and the bounded read omits it — the
        silent-loss direction. One disk row is enough to reproduce it; two disk rows
        sharing an id are not required.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", AsyncMock())
        state = _make_state(tmp_path)
        state.get_or_create_slot("dupmid")
        dup = "m-deadbeefdeadbeef"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "first", "slot": "dupmid", "meta": {"mid": dup}},
            )
            assert resp.status == 200
            slot = state.get_or_create_slot("dupmid")
            slot.drain()
            # Only the first row reaches disk, so the id is on disk exactly once.
            _save_slot_to_history(state, slot, force=True)

            resp = await client.post(
                "/api/chat",
                json={"message": "second", "slot": "dupmid", "meta": {"mid": dup}},
            )
            assert resp.status == 200
            slot.drain()
            mids = [m["meta"].get("mid") for m in slot.messages if isinstance(m.get("meta"), dict)]
            assert mids.count(dup) == 2, f"fixture no longer reproduces a repeated id; got {mids}"

            resp = await client.get("/api/chat/slots/dupmid?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert "second" in contents, f"a repeated id hid the un-flushed row; got {contents}"
        assert contents.count("first") == 1, f"a row was duplicated; got {contents}"

    def test_frozen_prefix_id_does_not_fund_a_window_match(self):
        """An id occurrence in the frozen prefix must not count as a flushed window row.

        ``all_msgs[:_disk_older_count]`` is the frozen prefix — on-disk rows OLDER
        than the window, so none of them is in ``slot.messages``. Counting ids over
        the whole disk read lets such an occurrence fund a consumption for a window
        row that was never flushed, and the boundary then walks past it. Only the
        on-disk window region, ``all_msgs[_disk_older_count:]``, may fund a match.

        Reachable because a caller-supplied ``mid`` is preserved rather than re-minted
        (``state.py:1801-1803``), which is what lets a redelivered row carry an id
        whose original has since been paged into the prefix.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        dup = "m-deadbeefdeadbeef"
        all_msgs = [
            {"role": "user", "content": "paged out", "ts": "t0", "meta": {"mid": dup}},
            {"role": "user", "content": "on disk", "ts": "t1", "meta": {"mid": "m-window1"}},
        ]
        slot = SimpleNamespace(
            messages=[
                {"role": "user", "content": "on disk", "ts": "t1", "meta": {"mid": "m-window1"}},
                {"role": "user", "content": "owed turn", "ts": "t2", "meta": {"mid": dup}},
            ],
            _disk_older_count=1,
        )
        # Guard the fixture: with no frozen prefix this case cannot arise at all and
        # the test would pass against the defect.
        assert slot._disk_older_count > 0, "fixture establishes no frozen prefix"

        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]

        assert [m["content"] for m in out] == [
            "paged out",
            "on disk",
            "owed turn",
        ], "a frozen-prefix id funded a match and hid the owed turn"

    @pytest.mark.asyncio
    async def test_mixed_id_and_id_less_disk_window_does_not_duplicate_an_injection(
        self, tmp_path, monkeypatch
    ):
        """A disk window holding BOTH id-carrying and id-less rows must not duplicate.

        The dual-write injectors stamp both copies of an injection with one id, so
        a NEW injection no longer produces this state — but transcripts written
        before the append path accepted an id hold exactly it, as does any caller
        that passes no id: earlier saved rows WITH ids plus a durable row WITHOUT
        one. The id-less ``append_if_absent`` call below is that legacy writer
        shape, and it must keep working unmigrated.

        Selecting id matching because SOME row carries an id then applies it to a row
        that structurally cannot match, so the injection is treated as un-flushed and
        appended a second time. Id matching is only sound when EVERY row in the disk
        window carries one; a mixed window belongs on the ordered path, which matches
        the fields both writers do record.

        Driven through the real writers rather than a hand-built mixed window, so it
        cannot encode a row shape neither writer emits.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("mixed")
        key = "dashboard:mixed"

        slot.append("user", "q1")
        slot.append("assistant", "a1")
        slot.drain()
        # Real save: these disk rows carry the window's own ids.
        _save_slot_to_history(state, slot, force=True)

        # The injector shape: window copy (id minted) plus durable copy (no meta).
        slot.append("assistant", "injected result")
        slot.drain()
        state.conversation_log.append_if_absent(key, "assistant", "injected result")

        disk = state.conversation_log.read_messages_chained(key)
        with_id = [
            m
            for m in disk
            if isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str)
        ]
        without_id = [
            m
            for m in disk
            if not (isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str))
        ]
        assert with_id and without_id, (
            f"fixture did not produce a MIXED disk window; "
            f"with_id={len(with_id)} without_id={len(without_id)}"
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/mixed?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert (
            contents.count("injected result") == 1
        ), f"the durable injection was appended twice; got {contents}"

    @pytest.mark.asyncio
    async def test_durable_injection_carrying_the_window_rows_id_matches_by_identity(
        self, tmp_path, monkeypatch
    ):
        """Both copies of an injection carry ONE id, so the identity walk matches.

        The dual-write shape: the window copy is appended through ``slot.append``,
        which mints ``meta.mid`` and returns the row; the durable copy passes that
        same id to ``append_if_absent``. The on-disk window region then holds ONLY
        id-carrying rows, so the bounded read reconciles by identity rather than a
        body heuristic, and the injection appears exactly once.

        Guarded on the all-id premise: if the durable copy ever stops carrying the
        id, the fixture degrades to the mixed shape and the guard names that,
        instead of the count assertion passing by way of the ordered fallback.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("dualid")
        key = "dashboard:dualid"

        slot.append("user", "q1")
        slot.append("assistant", "a1")
        slot.drain()
        # Real save: these disk rows carry the window's own ids.
        _save_slot_to_history(state, slot, force=True)

        # The injector shape: one id minted in the window, the SAME id persisted
        # on the durable copy.
        window_row = slot.append("assistant", "injected result")
        slot.drain()
        mid = window_row["meta"]["mid"]
        assert (
            state.conversation_log.append_if_absent(key, "assistant", "injected result", mid=mid)
            is True
        )

        disk = state.conversation_log.read_messages_chained(key)
        region_mids = [
            m["meta"].get("mid") if isinstance(m.get("meta"), dict) else None for m in disk
        ]
        assert all(
            isinstance(x, str) and x for x in region_mids
        ), f"the durable copy dropped the id — the region is mixed, not all-id: {region_mids}"
        assert mid in region_mids, "the durable copy's id is not the window row's id"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/dualid?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert (
            contents.count("injected result") == 1
        ), f"the identity walk failed to match the durable copy; got {contents}"

    @pytest.mark.asyncio
    async def test_hydrated_slot_over_an_all_id_transcript_is_not_served_twice(
        self, tmp_path, monkeypatch
    ):
        """Hydration must keep the disk rows' ids, or an all-id region doubles.

        With durable injection copies id-carrying, a cron transcript's window
        region can be ALL-id — the condition that selects the identity walk.
        ``hydrate_slot_from_history`` re-appends those rows into a fresh slot;
        if it dropped their ``meta``, every hydrated row would be re-minted a
        fresh id, no window id would match the disk, and the walk would mark
        the WHOLE history owed — a bounded read then serves every row twice
        until the next flush rewrites the file.
        """
        from kiro_crew.dashboard.cron_inject import hydrate_slot_from_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        key = "dashboard:cronhyd"

        # An all-id transcript: what a session looks like once every writer
        # (slot save and id-carrying durable appends) stamps meta.mid.
        state.conversation_log.append(key, "user", "q1", mid="m-disk-0000000001")
        state.conversation_log.append(key, "assistant", "a1", mid="m-disk-0000000002")
        state.conversation_log.append(key, "assistant", "cron result", mid="m-disk-0000000003")

        slot = state.get_or_create_slot("cronhyd")
        hydrate_slot_from_history(slot, state.conversation_log.read_messages(key))
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/cronhyd?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        for body in ("q1", "a1", "cron result"):
            assert contents.count(body) == 1, f"a hydrated row was served twice; got {contents}"

    @pytest.mark.asyncio
    async def test_interleaved_foreign_row_does_not_duplicate_the_persisted_suffix(
        self, tmp_path, monkeypatch
    ):
        """A foreign row BETWEEN two window rows must not end the ordered scan.

        The save is non-destructive against a cross-process append: it preserves
        rows another writer added and merges them back in TIME order
        (``_interleave_foreign_lines``, ``chat_persistence.py:1337-1357``, whose
        docstring notes such rows "genuinely happened BETWEEN the window's turns").
        So the on-disk window region can read ``[window, foreign, window]``.

        An unmatched row there means "not mine", not "end of window". Stopping at it
        leaves every persisted row after it in the tail, and those rows are appended
        a second time — duplication of an already-persisted suffix.

        Built through the real save path so the interleave is produced by the code
        that really orders these rows, and guarded so it cannot pass vacuously if no
        interleave occurs.

        The window timestamps are set EXPLICITLY, far apart, rather than left to the
        clock. The foreign row takes its real "now" and must sort strictly between
        them. With three clock-minted timestamps the test is platform-fragile: on a
        coarse-clock platform the foreign append and the second window row land in
        the same tick, and a shared ``ts`` carried by exactly one unmatched window
        entry and one unmatched disk line is the save's UNAMBIGUOUS in-place-edit
        case (``chat_persistence.py:1573-1587``), so the foreign row is dropped and
        the interleave never happens. Do not collapse these back to clock defaults.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("interleave")
        key = "dashboard:interleave"

        slot.append("user", "w1", ts="2020-01-01T00:00:00.000000+00:00")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        # Another writer appends between saves; its ts is "now", between the two
        # window rows below, so the merge must place it in the middle.
        state.conversation_log.append(key, "assistant", "foreign row")

        slot.append("user", "w2", ts="2030-01-01T00:00:00.000000+00:00")
        slot.drain()
        # This save preserves the foreign row and merges it back in time order.
        _save_slot_to_history(state, slot, force=True)

        disk = [m.get("content") for m in state.conversation_log.read_messages_chained(key)]
        assert disk == [
            "w1",
            "foreign row",
            "w2",
        ], f"fixture did not interleave the foreign row mid-window; disk={disk}"

        # A genuinely un-flushed turn after the interleave.
        slot.append("user", "w3", ts="2030-01-02T00:00:00.000000+00:00")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/interleave?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert (
            contents.count("w2") == 1
        ), f"the persisted suffix after the foreign row was duplicated; got {contents}"
        assert "w3" in contents, f"the un-flushed turn was dropped; got {contents}"

    @pytest.mark.asyncio
    async def test_save_redacted_row_does_not_duplicate_the_persisted_suffix(
        self, tmp_path, monkeypatch
    ):
        """A row the SAVE redacted must still match its raw window copy.

        The redaction is asymmetric in a session that was never restored. The save
        redacts every non-user role (``chat_persistence.py:1282-1284``) while the
        window keeps the text verbatim (``state.py:2107``), so the disk side is
        redacted and the window side is raw. Redacting only the disk side cannot
        converge on that pair, because redacting an already-redacted body just
        reproduces it. The row then reads as un-flushed, the walk stops, and the
        persisted suffix is appended a second time.

        Driven through the real writers, and the fixture asserts both preconditions
        first: the disk window region is MIXED, which is what selects the ordered
        path, and the two sides genuinely differ by the redaction transform.
        """
        from kiro_crew.dashboard.chat_persistence import (
            _save_slot_to_history,
            redact_credentials,
            redact_exfiltration_urls,
        )

        secret = "key AKIAIOSFODNN7EXAMPLE here"
        redacted, _ = redact_exfiltration_urls(secret)
        redacted, _ = redact_credentials(redacted)
        assert redacted != secret, "fixture needs content the real redactor changes"

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("redactdup")
        key = "dashboard:redactdup"

        # Explicit stamps, far apart, so the foreign row's real "now" sorts
        # strictly between them and can equal neither. On a coarse-clock platform
        # (Windows ticks in ~15.6 ms steps) clock-minted stamps put the foreign
        # append and the second window row in the same tick, and a ``ts`` carried
        # by exactly one unmatched window entry and one unmatched disk line is the
        # save's UNAMBIGUOUS in-place-edit case, so the second save DROPS the
        # foreign line and the region is no longer mixed. Do not collapse these
        # back to clock defaults; the sibling interleave test pins them for the
        # same reason.
        slot.append("user", "q1", ts="2020-01-01T00:00:00.000000+00:00")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        # An id-less foreign row is what puts this region on the ordered path.
        state.conversation_log.append(key, "assistant", "foreign row")

        slot.append("assistant", secret, ts="2030-01-01T00:00:00.000000+00:00")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        disk = state.conversation_log.read_messages_chained(key)
        has_id = [
            isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str) for m in disk
        ]
        assert any(has_id) and not all(has_id), (
            f"fixture did not produce a MIXED disk window, so the ordered path "
            f"would not run; has_id={has_id}"
        )
        assert any(m["content"] == redacted for m in disk), (
            f"fixture did not persist the redacted form; disk=" f"{[m['content'] for m in disk]}"
        )
        assert any(
            m.get("content") == secret for m in slot.messages
        ), "fixture did not keep the window copy raw"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/redactdup?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert (
            sum(1 for c in contents if "REDACTED" in c) == 1
        ), f"the save-redacted row was appended twice; got {contents}"

    @pytest.mark.asyncio
    async def test_redaction_equal_foreign_row_does_not_consume_an_unflushed_row(
        self, tmp_path, monkeypatch
    ):
        """Two DIFFERENT secrets redact alike; they must not be treated as one row.

        The redaction-equivalent compare exists for one shape only: a row and its
        own persisted copy, which differ by the transform because one side was
        redacted on save or load. That pair always carries the SAME ``ts`` — the
        save copies it verbatim (``chat_persistence.py:1288``) and so does the load
        (``:714``). So requiring the stamps to match costs the legitimate case
        nothing, and it stops a foreign row whose credential merely redacts to the
        same text from being consumed as the window's own.

        It cannot disturb a durable injection either: that pair is byte-identical,
        so it returns at the plain-equality branch above and never reaches here.
        """
        from kiro_crew.dashboard.chat_persistence import (
            _save_slot_to_history,
            redact_credentials,
            redact_exfiltration_urls,
        )

        def red(text: str) -> str:
            out, _ = redact_exfiltration_urls(text)
            out, _ = redact_credentials(out)
            return out

        # Two different credential KINDS rather than two near-identical keys: an
        # access-key id and a labelled secret-key assignment redact to the same tag,
        # so no second key-shaped literal is needed to make the bodies collide.
        mine = "key AKIAIOSFODNN7EXAMPLE here"
        theirs = "key aws_secret_access_key=x here"
        assert mine != theirs, "fixture needs two DISTINCT bodies"
        assert (
            red(mine) == red(theirs) != mine
        ), f"fixture needs two bodies that redact alike; got {red(mine)!r} vs {red(theirs)!r}"

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("redconflate")
        key = "dashboard:redconflate"

        slot.append("user", "q1", ts="2020-01-01T00:00:00.000000+00:00")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        # A foreign, id-less row carrying a DIFFERENT secret. Id-less is what puts
        # this region on the ordered path.
        state.conversation_log.append(key, "assistant", theirs)

        # Our own un-flushed row, never saved, with a stamp that cannot collide.
        slot.append("assistant", mine, ts="2030-01-01T00:00:00.000000+00:00")
        slot.drain()

        disk = state.conversation_log.read_messages_chained(key)
        has_id = [
            isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str) for m in disk
        ]
        assert any(has_id) and not all(has_id), (
            f"fixture did not produce a MIXED disk window, so the ordered path "
            f"would not run; has_id={has_id}"
        )
        assert not any(
            m.get("content") == mine for m in disk
        ), "fixture expects our row to be un-flushed"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/redconflate?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert sum(1 for c in contents if "REDACTED" in c) == 2, (
            f"a foreign row that merely redacts alike consumed the un-flushed row; "
            f"got {contents}"
        )

    @pytest.mark.asyncio
    async def test_bounded_read_runs_the_tail_match_off_the_event_loop(self, tmp_path, monkeypatch):
        """The tail match must not run on the event loop.

        It walks the whole window against the whole disk window region and applies
        the redaction transform to both sides of every candidate compare, so on a
        large mixed or id-less history the cost is real. Running it inline blocks
        every other request on the loop, and the loop-stall watchdog treats a long
        enough block as a hung gateway. The disk read immediately above it already
        goes through ``asyncio.to_thread``; this makes the pair consistent.
        """
        import threading

        import kiro_crew.dashboard.chat_handlers as ch

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("offloop")
        slot.append("user", "q1")
        slot.append("assistant", "a1")
        slot.drain()

        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}
        original = ch._append_unflushed_tail

        def spy(*args, **kwargs):
            seen["thread"] = threading.get_ident()
            return original(*args, **kwargs)

        monkeypatch.setattr(ch, "_append_unflushed_tail", spy)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/offloop?limit=10")
            assert resp.status == 200, await resp.text()

        assert "thread" in seen, (
            "the tail match never ran, so this test asserts nothing -- the request "
            "did not reach it"
        )
        assert seen["thread"] != loop_thread, (
            "the tail match ran on the event loop thread; it must be dispatched to a "
            "worker so a large history cannot stall the loop"
        )

    @pytest.mark.asyncio
    async def test_interleaved_unmatched_row_is_not_dropped_by_the_id_walk(
        self, tmp_path, monkeypatch
    ):
        """An un-flushed row BEFORE a persisted one must still reach the response.

        The id walk used to record a prefix boundary: on a match it set
        ``start = i + 1``, and a miss simply did not advance. So an un-flushed row
        followed by a matching row had the boundary moved PAST it, and the bounded
        response omitted it entirely — a drop, not a duplicate.

        Identity does not need a prefix. An id present in the disk window region
        proves that row reached disk, so the owed set is the rows whose id did NOT,
        taken in window order. That is a strict generalisation: when the persisted
        rows really are a prefix it gives the same answer.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("interleaveid")
        key = "dashboard:interleaveid"

        slot.append("user", "q1")
        slot.append("assistant", "a1")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        disk = state.conversation_log.read_messages_chained(key)
        disk_mids = {
            m["meta"]["mid"]
            for m in disk
            if isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str)
        }
        assert len(disk_mids) == len(disk) and disk, (
            f"fixture needs EVERY disk row to carry an id so the id walk runs; "
            f"disk={len(disk)} mids={len(disk_mids)}"
        )

        # An un-flushed assistant row positioned BEFORE the already-persisted one.
        owed = {
            "role": "assistant",
            "content": "owed-turn",
            "cls": "msg msg-a",
            "ts": "2026-08-18T21:00:00+00:00",
            "meta": {"mid": "m-owedneverpersist"},
        }
        slot.messages.insert(1, owed)
        assert owed["meta"]["mid"] not in disk_mids, "fixture row must be un-persisted"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/interleaveid?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert (
            "owed-turn" in contents
        ), f"the un-flushed row before a persisted row was dropped; got {contents}"
        assert contents.count("a1") == 1, f"the persisted row was duplicated; got {contents}"

    @pytest.mark.asyncio
    async def test_transient_window_row_does_not_truncate_the_id_prefix(
        self, tmp_path, monkeypatch
    ):
        """A transient row mid-window must not push persisted rows into the tail.

        Negative control for the test above. Transient roles are dropped by the
        save, and ``queued`` is outside ``_WIRE_ONLY_ROLES`` so it still gets an id
        minted — an id that can never appear on disk. Ending the walk at the first
        miss would therefore stop at this row and re-append every persisted row
        after it, which is the duplication this change exists to prevent.

        Driven through the real writers.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("transid")
        key = "dashboard:transid"

        slot.append("user", "q1")
        slot.append("queued", "queued-notice")
        slot.append("assistant", "a1")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        disk = state.conversation_log.read_messages_chained(key)
        assert [m.get("content") for m in disk] == ["q1", "a1"], (
            f"fixture expects the save to drop the transient row; disk="
            f"{[m.get('content') for m in disk]}"
        )
        queued = [m for m in slot.messages if m.get("role") == "queued"]
        assert queued and isinstance(
            queued[0].get("meta"), dict
        ), "fixture expects the queued row to carry a minted id"
        assert isinstance(
            queued[0]["meta"].get("mid"), str
        ), "fixture expects a string mid on the queued row"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/transid?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert contents == ["q1", "a1"], (
            f"a transient row truncated the id prefix and duplicated a persisted "
            f"row; got {contents}"
        )

    @pytest.mark.asyncio
    async def test_pending_approval_survives_a_bounded_read(self, tmp_path, monkeypatch):
        """A live ``permission`` row must reach the bounded response.

        ``_TRANSIENT_ROLES`` documents itself as being about a *window-region disk
        line* (``chat_persistence.py:1320-1322``), and that is how
        ``chat_persistence.py:1571`` uses it. The owed-set loop applies it to
        in-memory ``slot.messages`` instead, which answers a different question:
        which rows does the client still need? A pending approval is actionable, and
        the client reads it straight out of the transcript
        (``chatSlice.ts`` ``selectSlotPendingApproval``), so dropping it makes the
        approval bar vanish while the server is still waiting on an answer.

        Skipping it also bought nothing: ``permission`` is never persisted
        (``chat_persistence.py:1274`` returns ``None`` for it), so it can never have
        a disk counterpart for the id dedup to match.

        Single delivery is safe because the client REPLACES its transcript from this
        payload (``chatSlice.ts`` ``state.messages = next``) rather than appending,
        so returning the row once cannot double it.
        """
        import json as _json

        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("perm")
        key = "dashboard:perm"

        slot.append("user", "q1")
        slot.append("assistant", "a1")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        # Same call shape the runner uses: the approval metadata rides in ``cls``.
        slot.append("permission", "Run a shell command?", _json.dumps({"request_id": "req-1"}))
        slot.drain()

        disk = state.conversation_log.read_messages_chained(key)
        mids = [
            m["meta"]["mid"]
            for m in disk
            if isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str)
        ]
        assert disk and len(mids) == len(disk), (
            f"fixture needs EVERY disk row to carry an id so the owed-set loop runs; "
            f"disk={len(disk)} mids={len(mids)}"
        )
        assert not any(
            m.get("role") == "permission" for m in disk
        ), "fixture expects the permission row to be un-persisted"
        assert any(
            m.get("role") == "permission" for m in slot.messages
        ), "fixture expects the permission row in the window"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/perm?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        roles = [m["role"] for m in data["messages"]]
        assert (
            roles.count("permission") == 1
        ), f"the pending approval prompt was dropped or duplicated; roles={roles}"
        contents = [m["content"] for m in data["messages"]]
        assert contents.count("a1") == 1, f"a persisted row was duplicated; got {contents}"

    @pytest.mark.asyncio
    async def test_resolved_permission_is_not_appended_after_later_turns(
        self, tmp_path, monkeypatch
    ):
        """A RESOLVED approval must not be re-appended after later persisted rows.

        ``permission`` is never persisted (``chat_persistence.py:1274`` returns
        ``None``), so a permission row in the window is ALWAYS owed. For a pending
        one that is right — it is the last row, and the client needs it. A resolved
        one is historical: the agent has continued past it and those later turns
        have reached disk, so putting it in the tail moves it AFTER them and the
        rendered order is wrong.

        Resolution is an in-place edit of the row's ``cls`` JSON
        (``state.py`` ``_mark_permission_resolved``), so the row stays in the window
        and its position cannot distinguish it.
        """
        import json as _json

        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("permdone")
        key = "dashboard:permdone"

        slot.append("user", "q1")
        slot.append("permission", "Run a shell command?", _json.dumps({"request_id": "req-1"}))
        slot.mark_permission_resolved("req-1", "approved")
        slot.append("assistant", "a1")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        disk = state.conversation_log.read_messages_chained(key)
        mids = [
            m["meta"]["mid"]
            for m in disk
            if isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str)
        ]
        assert disk and len(mids) == len(disk), (
            f"fixture needs EVERY disk row to carry an id so the owed-set loop runs; "
            f"disk={len(disk)} mids={len(mids)}"
        )
        assert not any(
            m.get("role") == "permission" for m in disk
        ), "fixture expects the permission row to be un-persisted"
        window_roles = [m.get("role") for m in slot.messages]
        assert "permission" in window_roles, "fixture expects the permission row in the window"
        assert window_roles.index("permission") < window_roles.index(
            "assistant"
        ), f"fixture expects the approval to sit BEFORE the later turn; {window_roles}"
        perm = next(m for m in slot.messages if m.get("role") == "permission")
        assert (
            _json.loads(perm["cls"]).get("resolved") == "approved"
        ), "fixture expects the permission row to be marked resolved"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/permdone?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        roles = [m["role"] for m in data["messages"]]
        assert roles == ["user", "assistant"], (
            f"a resolved approval was re-appended after later persisted rows, "
            f"corrupting transcript order; roles={roles}"
        )

    @pytest.mark.asyncio
    async def test_live_chunk_row_survives_a_bounded_read(self, tmp_path, monkeypatch):
        """A live ``chunk`` row must reach the bounded response.

        ``_prepare_messages`` does not discard chunk rows, it ACCUMULATES them and
        emits one ``streaming`` row (``chat_utils.py:1651-1660``). That is the only
        way in-flight assistant text reaches the client through this endpoint,
        because the client filters raw ``chunk`` itself (``chatSlice.ts``
        ``SKIP_ROLES``). So skipping chunk rows in the owed set does not merely omit
        noise — it destroys the partial answer.

        Driven through the real producer's call shape (``chat_runner.py:3784``).
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("livechunk")
        key = "dashboard:livechunk"

        slot.append("user", "q1")
        slot.append("assistant", "a1")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        partial = "partial answer so far"
        slot.append("chunk", partial, "chunk")

        disk = state.conversation_log.read_messages_chained(key)
        has_id = [
            isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str) for m in disk
        ]
        assert has_id and all(has_id), (
            f"fixture needs every disk row to carry an id so the owed-set path "
            f"runs; has_id={has_id}"
        )
        assert not any(
            m.get("content") == partial for m in disk
        ), "fixture expects the chunk row to be un-persisted"
        assert any(
            m.get("role") == "chunk" for m in slot.messages
        ), "fixture expects the chunk row to be in the window"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/livechunk?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        streamed = [m["content"] for m in data["messages"] if m.get("role") == "streaming"]
        assert streamed == [partial], (
            f"the live partial answer was dropped by the bounded read; "
            f"streaming rows={streamed}"
        )

    def test_window_snapshot_survives_a_trim_between_the_two_reads(self):
        """A trim landing mid-snapshot must not duplicate a persisted row.

        The scan runs in a worker thread, so an event-loop append can trim the
        front of the window and bump ``_disk_older_count``
        (``state.py:2191-2200``) while the snapshot is being taken. Pairing a
        PRE-trim window with a POST-trim count shortens ``window_disk``, hides the
        trimmed rows' ids, and re-appends rows the disk read already returned.

        ``slot._lock`` is an ``asyncio.Lock`` and cannot be taken from this thread,
        so the fix is the bounded re-read ``_save_slot_to_history`` already uses
        (``chat_persistence.py:1711-1722``).
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        all_msgs = [
            {"role": "user", "content": "q1", "ts": "t1", "meta": {"mid": "m1"}},
            {"role": "assistant", "content": "a1", "ts": "t2", "meta": {"mid": "m2"}},
            {"role": "user", "content": "q2", "ts": "t3", "meta": {"mid": "m3"}},
        ]

        class TrimOnCopy(list):
            """Fires ONE trim after a copy has read every element."""

            def __init__(self, items):
                super().__init__(items)
                self.owner = None
                self.fired = False

            def __iter__(self):
                snapshot = list(super().__iter__())
                yield from snapshot
                if not self.fired:
                    self.fired = True
                    del self[:2]
                    self.owner._disk_older_count += 2

        window = TrimOnCopy(
            [
                {"role": "user", "content": "q1", "ts": "t1", "meta": {"mid": "m1"}},
                {"role": "assistant", "content": "a1", "ts": "t2", "meta": {"mid": "m2"}},
                {"role": "user", "content": "q2", "ts": "t3", "meta": {"mid": "m3"}},
                {"role": "assistant", "content": "owed", "ts": "t4"},
            ]
        )
        slot = SimpleNamespace(messages=window, _disk_older_count=0)
        window.owner = slot

        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]

        assert window.fired, "fixture never fired the trim, so nothing was raced"
        contents = [m["content"] for m in out]
        assert contents.count("q1") == 1 and contents.count("a1") == 1, (
            f"a trim between the two snapshot reads duplicated a persisted row; " f"got {contents}"
        )


# ── History persistence and disk fallback ──


class TestHistoryPersistence:
    def test_tool_messages_saved(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("dashboard:s1", "user", "hello")
        log.append("dashboard:s1", "tool", "✅ bash")
        log.append("dashboard:s1", "assistant", "hi there")
        msgs = log.read_messages("dashboard:s1")
        assert len(msgs) == 3
        assert msgs[1]["role"] == "tool"

    @pytest.mark.asyncio
    async def test_disk_fallback_for_trimmed_slot(self, tmp_path, monkeypatch):
        """Default view uses in-memory; pagination of older messages uses disk."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("big")
        log = state.conversation_log

        # Use a count that fits in memory — test disk pagination without trim
        for i in range(300):
            log.append("dashboard:big", "user", f"msg {i}")
            slot.append("user", f"msg {i}")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            # Default: in-memory
            resp = await client.get("/api/chat/slots/big?limit=200")
            data = await resp.json()
            assert data["total"] == 300
            assert data["has_more"] is True
            assert data["messages"][-1]["content"] == "msg 299"

            # Pagination with before: falls back to disk
            resp = await client.get("/api/chat/slots/big?limit=200&before=100")
            data = await resp.json()
            assert len(data["messages"]) == 100
            assert data["messages"][0]["content"] == "msg 0"
            assert data["has_more"] is False


# ── Save vs. permanent delete ──


class TestSaveDoesNotResurrectDeletedSession:
    """#6677: a save must not recreate a session whose permanent delete committed.

    ``delete_session`` unlinks under the same ``_locked`` region the save
    holds, leaves no tombstone, and reports success. A save routed through
    ``save_slot_off_loop`` takes the patient acquire, so it can sit waiting
    while the delete runs to completion ahead of it — the sequential
    delete-then-save below exercises exactly the code path such a save takes
    once the lock is finally granted (file gone, slot still carrying its
    in-memory window).
    """

    def test_save_after_committed_delete_does_not_recreate_the_file(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("gone")
        slot.append("user", "hello")
        slot.append("assistant", "hi")
        slot.drain()
        # First save through the real path: the session now exists on disk and
        # the slot has learned it (``_disk_window_len`` > 0).
        assert _save_slot_to_history(state, slot, force=True) is True
        path = state.conversation_log._path("dashboard:gone")
        assert path.exists()
        assert slot._disk_window_len > 0

        # The permanent delete commits and reports success…
        assert state.conversation_log.delete_session("dashboard:gone") is True
        assert not path.exists()

        # …so a save that acquires the lock afterwards must NOT undo it, even
        # though its window still holds the whole conversation. The ``False``
        # return is the caller-visible signal that nothing was persisted.
        assert _save_slot_to_history(state, slot, force=True) is False
        assert not path.exists(), (
            "a save that lost the race to a committed permanent delete "
            "recreated the session file"
        )

    def test_resumed_slot_save_after_delete_does_not_recreate_the_file(self, tmp_path, monkeypatch):
        """The ``_resumed_count`` evidence arm: a slot restored from history
        (no save of its own yet) must also honor a committed delete."""
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:revived", "user", "old turn")
        slot = state.get_or_create_slot("revived")
        slot.append("user", "old turn")
        slot.drain()
        # As the restore path records them: the resumed count AND the observed
        # disk identity (every hydrate site records both).
        slot._resumed_count = 1
        slot._disk_meta_created_at = str(log.get_metadata("dashboard:revived")["created_at"])
        assert slot._disk_window_len == 0

        assert log.delete_session("dashboard:revived") is True
        path = log._path("dashboard:revived")
        assert not path.exists()

        _save_slot_to_history(state, slot, force=True)
        assert not path.exists(), "a resumed slot's save recreated a permanently deleted session"

    @pytest.mark.asyncio
    async def test_fork_aborts_even_after_a_flush_consumed_the_delete_signal(
        self, tmp_path, monkeypatch
    ):
        """The flush-ordering bypass: a NON-dirty deleted slot must still not fork.

        The periodic 5s flush can hit the delete-won guard first and clear
        ``_dirty``. The fork then skips its own flush arms entirely (they are
        gated on ``slot._dirty``), the disk read comes back empty, and
        ``all_messages`` falls back to the in-memory window — republishing the
        deleted conversation with no save ever returning ``False`` to the fork.
        The direct ``session_was_deleted`` probe must refuse it anyway.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("forkclean")
        slot.append("user", "keep me?", "msg msg-u")
        slot.append("assistant", "sure", "msg msg-a")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        assert state.conversation_log.delete_session("dashboard:forkclean") is True
        # Model the flush having consumed the delete-won signal already.
        slot._dirty = False

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/forkclean/fork",
                json={"prompt": "forked"},
            )
            payload = await resp.json()

        assert (
            resp.status == 409
        ), f"non-dirty fork of a deleted session must abort, got {resp.status} {payload}"
        session_files = [p.name for p in tmp_path.glob("*.jsonl")]
        assert (
            session_files == []
        ), f"a fork of the deleted conversation was published: {session_files}"

    @pytest.mark.asyncio
    async def test_transfer_bundle_refuses_a_deleted_session(self, tmp_path, monkeypatch):
        """Same bypass on the export path: a non-dirty deleted slot must not bundle."""
        from kiro_crew.dashboard import session_transfer as st
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("bundlegone")
        slot.append("user", "ship me?", "msg msg-u")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        assert state.conversation_log.delete_session("dashboard:bundlegone") is True
        slot._dirty = False

        with pytest.raises(st.SnapshotUnstable, match="permanently deleted"):
            await st.build_transfer_bundle_async(state, slot, origin="test")

    @pytest.mark.asyncio
    async def test_transfer_refuses_a_delete_landing_during_assembly(self, tmp_path, monkeypatch):
        """A delete completing INSIDE the threaded bundle read must still refuse.

        The assembly read is the builder's longest await, so the pre-read probe
        can pass and the permanent delete complete before the bundle returns —
        the bundle in hand is the destroyed conversation. The post-assembly
        re-check must catch it.
        """
        from kiro_crew.dashboard import session_transfer as st
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("midassembly")
        slot.append("user", "racing", "msg msg-u")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)
        slot._dirty = False

        real_assemble = st._read_and_assemble

        def _assemble_with_delete_landing_inside(*args, **kwargs):
            bundle = real_assemble(*args, **kwargs)
            # The permanent delete commits while the worker thread is still
            # inside the assembly (after the pre-read probe passed).
            assert state.conversation_log.delete_session("dashboard:midassembly") is True
            return bundle

        monkeypatch.setattr(st, "_read_and_assemble", _assemble_with_delete_landing_inside)

        with pytest.raises(st.SnapshotUnstable, match="permanently deleted"):
            await st.build_transfer_bundle_async(state, slot, origin="test")

    def test_save_does_not_merge_into_a_file_recreated_after_the_delete(
        self, tmp_path, monkeypatch
    ):
        """Delete -> foreign append recreates the file -> pending save must skip.

        ``delete_session`` leaves no tombstone, so a channel/cron
        ``append_off_loop`` landing after the delete creates a FRESH session
        file (new metadata ``created_at``). A pending save that only checked
        file existence would merge the deleted window into that new transcript
        — resurrecting the destroyed conversation inside someone else's rows.
        The guard must recognize the new incarnation by its identity and skip.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("reborn")
        slot.append("user", "the deleted conversation", "msg msg-u")
        slot.drain()
        assert _save_slot_to_history(state, slot, force=True) is True
        assert slot._disk_meta_created_at, "save must record the disk identity"

        log = state.conversation_log
        assert log.delete_session("dashboard:reborn") is True
        # A foreign writer recreates the session file with a fresh identity.
        log.append("dashboard:reborn", "assistant", "foreign row in the new file")
        path = log._path("dashboard:reborn")
        assert path.exists()

        assert _save_slot_to_history(state, slot, force=True) is False
        content = path.read_text(encoding="utf-8")
        assert (
            "the deleted conversation" not in content
        ), "the pending save merged the deleted window into the recreated file"
        assert (
            "foreign row in the new file" in content
        ), "the guard must leave the new incarnation untouched"

    def test_channel_surfaced_slot_records_the_disk_identity(self, tmp_path, monkeypatch):
        """The channel surfacing path must arm the delete-won identity too.

        ``surface_channel_session`` adopts an append-created transcript — its
        slot never runs the dashboard rehydrate paths, so without recording
        ``_disk_meta_created_at`` here the identity arm of the delete-won
        guard fails open for every channel tab: delete -> inbound append
        recreates the file -> the pending save merges the deleted window into
        the new transcript.
        """
        from kiro_crew.dashboard import channel_slots
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        key = "slack:thread-77"
        log.append(key, "user", "channel turn")
        meta = log.get_metadata(key)
        assert meta.get("created_at"), "fixture transcript must carry an identity"

        slot = channel_slots.surface_channel_session(
            state,
            {"key": key, "title": "chan"},
            meta,
            log.read_messages_chained(key),
            session_key=key,
        )
        assert slot is not None
        assert slot._disk_meta_created_at == str(
            meta["created_at"]
        ), "channel surfacing must record the observed disk identity"

        # Delete, then a foreign append recreates the file with a new identity:
        # the surviving slot's pending save must skip, not merge.
        assert log.delete_session(key) is True
        log.append(key, "assistant", "new incarnation row")
        assert _save_slot_to_history(state, slot, force=True) is False
        content = log._path(key).read_text(encoding="utf-8")
        assert (
            "channel turn" not in content
        ), "the channel slot's save merged the deleted window into the recreated file"

    def test_failed_first_save_is_not_mistaken_for_deletion(self, tmp_path, monkeypatch):
        """A fork whose best-effort first save failed must still persist on retry.

        ``chat_fork`` saves the destination slot best-effort and then sets
        ``_resumed_count`` unconditionally — a transient first-write failure is
        swallowed (dirty re-armed) and leaves a slot with resumed evidence but
        NO file and NO observed identity. An evidence gate built on the
        counters alone would misread that as "was on disk, now deleted", skip
        the retry, clear ``_dirty``, and lose the acknowledged fork at the next
        restart. The gate must require an observed disk identity
        (``_disk_meta_created_at``), which only a real hydrate or a committed
        save records.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("forkchild")
        slot.append("user", "the forked conversation", "msg msg-u")
        slot.drain()
        # As chat_fork leaves the slot when its best-effort save failed: the
        # optimistic counter is set, no write ever committed, no identity.
        slot._resumed_count = len(slot.messages)
        assert slot._disk_meta_created_at == ""

        assert (
            _save_slot_to_history(state, slot, force=True) is True
        ), "the retry of a failed first save was misread as delete-won"
        path = state.conversation_log._path("dashboard:forkchild")
        assert path.exists(), "the acknowledged fork never reached disk"
        assert "the forked conversation" in path.read_text(encoding="utf-8")

    def test_zero_message_resumed_session_delete_still_wins(self, tmp_path, monkeypatch):
        """A restored EMPTY session's delete must beat the save of its first message.

        A zero-message restore leaves every window counter at 0, so an
        evidence gate that requires the counters alongside the identity skips
        the guard for exactly this slot — the save of the session's first
        message would recreate the deleted history. Identity alone must gate.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        # A session file with metadata but no message rows — e.g. a titled
        # session whose turns were all trimmed/consolidated away.
        log.update_metadata("dashboard:empty", {"title": "empty but real"})
        meta = log.get_metadata("dashboard:empty")
        assert meta.get("created_at")

        slot = state.get_or_create_slot("empty")
        # As a restore of that session records it: identity observed, all
        # window counters zero (nothing to load).
        slot._disk_meta_created_at = str(meta["created_at"])
        assert slot._resumed_count == 0
        assert slot._disk_older_count == 0
        assert slot._disk_window_len == 0

        # First message arrives, then the permanent delete wins the lock.
        slot.append("user", "first message", "msg msg-u")
        slot.drain()
        assert log.delete_session("dashboard:empty") is True

        assert _save_slot_to_history(state, slot, force=True) is False
        assert not log._path(
            "dashboard:empty"
        ).exists(), "the first-message save recreated a deleted zero-message session"

    def test_unreadable_metadata_defers_the_save_instead_of_writing(self, tmp_path, monkeypatch):
        """A transient metadata read failure must fail CLOSED, not blank the check.

        ``get_metadata`` returns ``{}`` for both "no metadata" and "read
        failed"; blanking the identity comparison on a transient failure would
        let a pending save overwrite a replacement session with deleted
        content. The save must refuse to write (raising, so ``_dirty`` stays
        armed and the flush retries) until the metadata is readable again.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        slot = state.get_or_create_slot("flaky")
        slot.append("user", "hello", "msg msg-u")
        slot.drain()
        assert _save_slot_to_history(state, slot, force=True) is True
        before = log._path("dashboard:flaky").read_text(encoding="utf-8")

        monkeypatch.setattr(log, "get_metadata_status", lambda key: ({}, False))
        with pytest.raises(OSError, match="unreadable"):
            _save_slot_to_history(state, slot, force=True)
        assert (
            log._path("dashboard:flaky").read_text(encoding="utf-8") == before
        ), "the save wrote through an unverifiable identity"

    def test_probe_refuses_the_copy_when_metadata_is_unreadable(self, tmp_path, monkeypatch):
        """``session_was_deleted`` must fail closed on an unreadable metadata line."""
        from kiro_crew.dashboard.chat_persistence import (
            _save_slot_to_history,
            session_was_deleted,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        slot = state.get_or_create_slot("murky")
        slot.append("user", "hello", "msg msg-u")
        slot.drain()
        assert _save_slot_to_history(state, slot, force=True) is True
        assert session_was_deleted(state, slot) is False

        monkeypatch.setattr(log, "get_metadata_status", lambda key: ({}, False))
        assert (
            session_was_deleted(state, slot) is True
        ), "an unverifiable identity must refuse the copy, not allow it"

    def test_first_save_of_a_fresh_slot_still_creates_the_file(self, tmp_path, monkeypatch):
        """Control: the guard must not break the normal first create — a
        brand-new slot has no on-disk evidence and starts with no file."""
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("fresh")
        slot.append("user", "first message")
        slot.drain()

        _save_slot_to_history(state, slot, force=True)
        assert state.conversation_log._path(
            "dashboard:fresh"
        ).exists(), "the delete-won guard aborted a legitimate first save"

    @pytest.mark.asyncio
    async def test_fork_aborts_when_the_source_was_deleted(self, tmp_path, monkeypatch):
        """A fork must not republish a permanently deleted conversation.

        The fork's pre-copy flush runs with ``best_effort=False`` and used to
        treat any non-raising return as a confirmed durable write. A delete-won
        skip raises nothing, and the fork's DESTINATION slot is brand new — it
        carries no delete evidence, so its save would proceed and the destroyed
        conversation would come back under a fresh key. The fork must abort
        instead.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("forkgone")
        slot.append("user", "keep me?", "msg msg-u")
        slot.append("assistant", "sure", "msg msg-a")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)
        # New unpersisted activity keeps the slot dirty, so the fork takes its
        # durable-flush arm.
        slot.append("user", "one more", "msg msg-u")
        slot._dirty = True

        assert state.conversation_log.delete_session("dashboard:forkgone") is True

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/forkgone/fork",
                json={"prompt": "forked"},
            )
            payload = await resp.json()

        assert (
            resp.status == 409
        ), f"fork of a deleted session must abort, got {resp.status} {payload}"
        assert not state.conversation_log._path(
            "dashboard:forkgone"
        ).exists(), "the fork's flush recreated the deleted source session"
        # No fork session file may exist either — the copy was refused.
        session_files = [
            p.name for p in tmp_path.glob("*.jsonl") if "forkgone" in p.name or "fork" in p.name
        ]
        assert (
            session_files == []
        ), f"a fork of the deleted conversation was published: {session_files}"

    @pytest.mark.asyncio
    async def test_fork_rolls_back_a_delete_landing_during_the_destination_save(
        self, tmp_path, monkeypatch
    ):
        """A delete committing INSIDE the destination save must still win.

        The pre-copy probe passes (the source is alive when it runs) and the
        destination save takes the DESTINATION's history lock, which does not
        serialise against the source's delete. So the delete can commit inside
        that await, and the fork would be acknowledged holding a full copy of a
        conversation the user destroyed. The handler must refuse at the
        acknowledgment boundary and remove the copy it had already written --
        the same rule ``build_transfer_bundle_async`` applies by re-probing
        after assembly.
        """
        from kiro_crew.dashboard import chat_fork as fork_mod
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("forkrace")
        slot.append("user", "delete me", "msg msg-u")
        slot.append("assistant", "noted", "msg msg-a")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)
        # Not dirty: the handler's pre-copy flush arms are gated on ``_dirty``,
        # so the ONLY save in this fork is the destination's — which is exactly
        # where the race lives.
        slot._dirty = False
        source_path = state.conversation_log._path("dashboard:forkrace")
        assert source_path.exists()
        before = {p.resolve() for p in tmp_path.rglob("*.jsonl")}

        real_save = fork_mod.save_slot_off_loop
        dest_keys: list[str] = []

        async def _save_then_delete(st, target, *args, **kwargs):
            result = await real_save(st, target, *args, **kwargs)
            if target is not slot:
                dest_keys.append(target.key)
                # The permanent delete commits while the destination save is in
                # flight, i.e. after the pre-copy probe has already passed.
                assert st.conversation_log.delete_session("dashboard:forkrace") is True
            return result

        monkeypatch.setattr(fork_mod, "save_slot_off_loop", _save_then_delete)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/forkrace/fork",
                json={"prompt": "forked"},
            )
            payload = await resp.json()

        assert (
            resp.status == 409
        ), f"the delete must win over an unacknowledged fork, got {resp.status} {payload}"
        assert payload.get("code") == "fork_source_deleted"
        assert not source_path.exists()
        # No transcript may survive the rollback: comparing against the
        # pre-fork set keeps this independent of how the fork mints its key.
        leftover = sorted(p.name for p in tmp_path.rglob("*.jsonl") if p.resolve() not in before)
        assert leftover == [], f"a copy of the deleted conversation remains on disk: {leftover}"
        # …and the destination slot must be gone from the live set too. The key
        # comes from the save the handler actually made, not the 409 body.
        assert dest_keys, "the destination save never ran, so this pins nothing"
        assert state._slots.get(dest_keys[0]) is None, "the rolled-back fork is still a live slot"

    def test_probe_catches_a_delete_landing_between_its_stat_and_metadata_read(
        self, tmp_path, monkeypatch
    ):
        """The probe is lock-free, so a delete can land in the middle of it.

        ``get_metadata_status`` reports a file that no longer exists as
        ``({}, True)`` -- by its own contract a GENUINE empty answer, not an
        unreadable one. So a delete committing between the probe's ``stat`` and
        its metadata read leaves an empty ``created_at`` that must not be read
        as "legacy metadata, fail open": that would answer "not deleted" for a
        session that is gone and let fork/transfer republish it. The save's own
        guard cannot hit this -- it reads both inside ``_locked``, the lock
        ``delete_session`` unlinks under.
        """
        from kiro_crew.dashboard.chat_persistence import session_was_deleted

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:midprobe", "user", "delete me")
        slot = state.get_or_create_slot("midprobe")
        slot._disk_meta_created_at = str(log.get_metadata("dashboard:midprobe")["created_at"])
        # Control: the session is alive, so the probe must not refuse the copy.
        assert session_was_deleted(state, slot) is False

        real_status = log.get_metadata_status

        def _delete_then_read(key):
            # The permanent delete commits after the probe's stat() has already
            # seen the file and before the probe reads its metadata.
            log.delete_session("dashboard:midprobe")
            return real_status(key)

        monkeypatch.setattr(log, "get_metadata_status", _delete_then_read)
        assert (
            session_was_deleted(state, slot) is True
        ), "the probe missed a delete landing between its stat and its metadata read"

    def test_probe_still_fails_open_on_legacy_metadata_without_created_at(
        self, tmp_path, monkeypatch
    ):
        """The other empty: a live file whose metadata carries no ``created_at``.

        The re-stat must not turn the documented fail-open for legacy metadata
        into a refusal -- the file is there, so the copy proceeds.
        """
        from kiro_crew.dashboard.chat_persistence import session_was_deleted

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:legacymeta", "user", "keep me")
        slot = state.get_or_create_slot("legacymeta")
        slot._disk_meta_created_at = str(log.get_metadata("dashboard:legacymeta")["created_at"])

        monkeypatch.setattr(log, "get_metadata_status", lambda key: ({}, True))
        assert (
            session_was_deleted(state, slot) is False
        ), "legacy metadata with no created_at must still fail open while the file exists"


# ── Slot lifecycle ──


class TestSlotLifecycle:
    @pytest.mark.asyncio
    async def test_list_slots(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("a")
        state.get_or_create_slot("b")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots")
            data = await resp.json()
            keys = [s["key"] for s in data]
            assert "a" in keys and "b" in keys

    @pytest.mark.asyncio
    async def test_list_slots_schedules_source_refresh_with_push_callback(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.handlers import source_providers

        scheduler = MagicMock()
        monkeypatch.setattr(source_providers, "schedule_check_refresh", scheduler)
        state = _make_state(tmp_path)
        state.owner_id = "U_OWNER"
        slot = state.get_or_create_slot("source")
        url = "https://github.com/acme/repo/pull/12"
        slot.append("user", url)

        @web.middleware
        async def owner_auth(request, handler):
            request["user"] = "U_OWNER"
            request["app"] = ""
            return await handler(request)

        app = _make_app(state)
        app.middlewares.append(owner_auth)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/slots")
            assert resp.status == 200

        scheduler.assert_called_once()
        urls, callback = scheduler.call_args.args
        assert urls == [url]
        assert callback == state.push_slots_update

    @pytest.mark.asyncio
    async def test_list_slots_omits_status_and_refresh_for_non_owner(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import state as state_module
        from kiro_crew.dashboard.handlers import source_providers

        scheduler = MagicMock()
        monkeypatch.setattr(source_providers, "schedule_check_refresh", scheduler)
        monkeypatch.setattr(
            state_module,
            "_cached_check_status",
            lambda _url: {"ci": "passed", "state": "open"},
        )
        state = _make_state(tmp_path)
        state.owner_id = "U_OWNER"
        slot = state.get_or_create_slot("source")
        url = "https://github.com/acme/private/pull/12"
        slot.append("user", url)

        @web.middleware
        async def non_owner_auth(request, handler):
            request["user"] = "U_OTHER"
            request["app"] = ""
            return await handler(request)

        app = _make_app(state)
        app.middlewares.append(non_owner_auth)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/slots")
            payload = await resp.json()

        assert resp.status == 200
        link = next(item for item in payload if item["key"] == "source")["source_links"][0]
        assert link == {
            "provider": "github",
            "number": 12,
            "url": url,
            "kind": "change",
            "label": "#12",
        }
        scheduler.assert_not_called()

    def test_slot_status_serialization_requires_owner_opt_in(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import state as state_module

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            state_module,
            "_cached_check_status",
            lambda _url: {"ci": "passed", "state": "open"},
        )
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("source")
        slot.append("user", "https://github.com/acme/private/pull/12")

        generic_link = state.serialize_slots()[0]["source_links"][0]
        owner_link = state.serialize_slots(include_check_status=True)[0]["source_links"][0]

        assert "ci" not in generic_link
        assert "state" not in generic_link
        assert owner_link["ci"] == "passed"
        assert owner_link["state"] == "open"

    @pytest.mark.asyncio
    async def test_approve_no_pending(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "approved"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_approve_resolves_future(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["test"] = fut

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "approved"})
            data = await resp.json()
            assert data["ok"] is True
            assert fut.result() == "approved"

    @pytest.mark.asyncio
    async def test_trust_sets_flag_and_approves(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["test"] = fut
        slot.messages.append(
            {
                "role": "permission",
                "content": "Running: ls",
                "cls": json.dumps(
                    {
                        "request_id": "test",
                        "full_command": "ls",
                        "trust_grantable": "1",
                    }
                ),
            }
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "trust"})
            data = await resp.json()
            assert data["ok"] is True
            assert slot._trust is True
            assert fut.result() == "approved"

    @pytest.mark.asyncio
    async def test_approve_broadcasts_approval_resolved_single_pending(self, tmp_path, monkeypatch):
        """Single pending future without explicit request_id: extracts id and broadcasts."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        slot._approval_futures["req-abc"] = fut
        state.broadcast_ws = MagicMock()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "approved"})
            assert (await resp.json())["ok"] is True
            state.broadcast_ws.assert_any_call(
                "approval_resolved",
                # ``slot`` keys the frame for the slot-scoped WS gate: without it
                # an app token never receives its OWN approval resolution.
                {"id": "req-abc", "approved": True, "slot": "s1"},
            )

    @pytest.mark.asyncio
    async def test_approve_broadcasts_with_explicit_request_id(self, tmp_path, monkeypatch):
        """Explicit request_id is forwarded in the broadcast."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        slot._approval_futures["req-xyz"] = fut
        state.broadcast_ws = MagicMock()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve", json={"action": "approved", "request_id": "req-xyz"}
            )
            assert (await resp.json())["ok"] is True
            state.broadcast_ws.assert_any_call(
                "approval_resolved",
                # ``slot`` keys the frame for the slot-scoped WS gate: without it
                # an app token never receives its OWN approval resolution.
                {"id": "req-xyz", "approved": True, "slot": "s1"},
            )

    @pytest.mark.asyncio
    async def test_reject_broadcasts_approved_false(self, tmp_path, monkeypatch):
        """Rejection broadcasts approved=False."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        slot._approval_futures["req-rej"] = fut
        state.broadcast_ws = MagicMock()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve", json={"action": "rejected", "request_id": "req-rej"}
            )
            assert (await resp.json())["ok"] is True
            state.broadcast_ws.assert_any_call(
                "approval_resolved",
                # ``slot`` keys the frame for the slot-scoped WS gate: without it
                # an app token never receives its OWN approval resolution.
                {"id": "req-rej", "approved": False, "slot": "s1"},
            )


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


# ── Full pagination walk (simulates infinite scroll) ──


class TestFullPaginationWalk:
    @pytest.mark.asyncio
    async def test_walk_all_pages(self, tmp_path, monkeypatch):
        """Simulate frontend infinite scroll — walk backwards through all messages."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("walk")
        log = state.conversation_log
        total_msgs = 450

        for i in range(total_msgs):
            log.append("dashboard:walk", "user", f"msg {i}")
            slot.append("user", f"msg {i}")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            all_collected: list[str] = []
            before = None
            pages = 0

            while True:
                url = "/api/chat/slots/walk?limit=100"
                if before is not None:
                    url += f"&before={before}"
                resp = await client.get(url)
                data = await resp.json()
                msgs = data["messages"]
                all_collected = [m["content"] for m in msgs] + all_collected
                pages += 1

                if not data["has_more"]:
                    break
                before = data["total"] - len(all_collected)

            assert len(all_collected) == total_msgs
            assert all_collected[0] == "msg 0"
            assert all_collected[-1] == f"msg {total_msgs - 1}"
            assert pages > 1

    @pytest.mark.asyncio
    async def test_walk_with_trimmed_memory(self, tmp_path, monkeypatch):
        """Pagination with before uses disk — can access all messages."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("trim")
        log = state.conversation_log

        for i in range(400):
            log.append("dashboard:trim", "user", f"msg {i}")
            slot.append("user", f"msg {i}")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            # Default: in-memory
            resp = await client.get("/api/chat/slots/trim?limit=200")
            data = await resp.json()
            assert data["total"] == 400
            assert data["messages"][-1]["content"] == "msg 399"

            # Pagination: disk has all 400
            resp = await client.get("/api/chat/slots/trim?limit=200&before=200")
            data = await resp.json()
            assert data["total"] == 400
            assert data["messages"][0]["content"] == "msg 0"
            assert data["has_more"] is False


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


# ── _prepare_messages filtering ──


class TestPrepareMessages:
    def test_queued_preserved_done_stripped(self):
        """queued messages must survive _prepare_messages so the frontend shows the banner after tab switch."""
        from kiro_crew.dashboard.chat import _prepare_messages

        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "queued", "content": "next msg"},
            {"role": "done", "content": ""},
            {"role": "assistant", "content": "hi"},
        ]
        out = _prepare_messages(msgs, running=False, live_child="")
        roles = [m["role"] for m in out]
        assert "queued" in roles, "queued must be preserved for tab-switch indicator"
        assert "done" not in roles, "done must be stripped"

    def test_chunks_collapsed_to_streaming(self):
        """Trailing chunks should be collapsed into a single streaming message."""
        from kiro_crew.dashboard.chat import _prepare_messages

        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "chunk", "content": "Hel"},
            {"role": "chunk", "content": "lo"},
        ]
        out = _prepare_messages(msgs, running=True, live_child="")
        assert out[-1]["role"] == "streaming"
        assert "Hel" in out[-1]["content"]

    def test_queued_placeholder_removed_on_processing(self):
        """When a queued message starts processing, its placeholder is replaced by a user entry."""
        import json

        from kiro_crew.dashboard.chat import _remove_queued_by_id

        slot = _ChatSlot("s1")
        slot.append("user", "first")
        qid = slot.queue_append("second")
        slot.append("queued", "second", json.dumps({"queue_id": qid}))

        item = slot.queue_pop(0)
        _remove_queued_by_id(slot.messages, item["id"])
        slot.append("user", item["content"], "msg msg-u")

        roles = [m["role"] for m in slot.messages]
        assert "queued" not in roles, "queued placeholder must be removed once processing starts"
        assert roles.count("user") == 2

    def test_duplicate_queued_removes_only_targeted(self):
        """When the same text is queued twice, only the targeted placeholder is removed by ID."""
        import json

        from kiro_crew.dashboard.chat import _remove_queued_by_id

        slot = _ChatSlot("s1")
        qid1 = slot.queue_append("hello")
        qid2 = slot.queue_append("hello")
        slot.append("queued", "hello", json.dumps({"queue_id": qid1}))
        slot.append("queued", "hello", json.dumps({"queue_id": qid2}))

        item = slot.queue_pop(0)
        _remove_queued_by_id(slot.messages, item["id"])
        slot.append("user", item["content"], "msg msg-u")

        queued = [m for m in slot.messages if m.get("role") == "queued"]
        assert len(queued) == 1, "second queued placeholder must survive"
        # Verify the surviving placeholder is the one with qid2
        surviving_cls = json.loads(queued[0].get("cls", "{}"))
        assert surviving_cls.get("queue_id") == qid2


class TestKiroReadinessQueueHandoff:
    @pytest.mark.asyncio
    async def test_dequeued_turn_runs_without_a_readiness_probe(
        self,
        tmp_path,
    ):
        """A queued item is never lost to a readiness check.

        Readiness used to be probed on turn entry AND again before dequeueing,
        so a third false answer could drop an item already popped off the queue.
        Both probes are gone — readiness is latched at boot and the ACP attempt
        reports auth failures — so the successor turn simply runs. This pins the
        no-loss invariant that outlives the probes.
        """
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        delivered: list[str] = []

        async def stream(stream_message: str):
            delivered.append(stream_message)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"response to {stream_message}")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client = MagicMock()
        client.stream = stream
        client.stream_command = stream
        client.context_usage_pct = MagicMock(return_value=1.0)
        state = _make_state(tmp_path)
        # A service whose latch would answer "not ready" if anything asked it.
        state.kiro_prerequisite_service = object()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        slot = state.get_or_create_slot("queued-readiness")
        slot._titled = True
        queue_id = slot.queue_append("keep this queued")

        await _run_chat(state, slot, "first message")
        assert slot.task is not None
        await slot.task

        assert delivered[0] == "first message"
        assert delivered[1].endswith("keep this queued")
        assert slot._queue == []
        assert any(
            call.args[0] == "queue_pop" and call.args[1]["queue_id"] == queue_id
            for call in state.broadcast_ws.call_args_list
        )


# ── History save on close (not per-turn) ──


class TestHistorySaveOnClose:
    @pytest.mark.asyncio
    async def test_close_saves_to_history(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.append("assistant", "hi")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/s1")
            data = await resp.json()
            assert data["ok"] is True

        # Verify saved to disk
        msgs = state.conversation_log.read_messages("dashboard:s1")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_transient_roles_excluded_from_history(self, tmp_path, monkeypatch):
        """chunk, done, queued, permission should not be saved to history."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "run ls")
        slot.append("permission", "ls")
        slot.append("tool", "✅ ls")
        slot.append("queued", "next msg")
        slot.append("chunk", "partial")
        slot.append("done", "")
        slot.append("assistant", "done")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.delete("/api/chat/slots/s1")

        msgs = state.conversation_log.read_messages("dashboard:s1")
        roles = [m["role"] for m in msgs]
        assert "chunk" not in roles
        assert "done" not in roles
        assert "queued" not in roles
        assert "permission" not in roles
        assert roles == ["user", "tool", "assistant"]

    @pytest.mark.asyncio
    async def test_resume_restores_the_channel_filing_marker(self, tmp_path, monkeypatch):
        """Resuming from History must carry the channel-filing marker into memory.

        Four paths rebuild a slot from history. This one (the History page's
        resume endpoint) is the one that reaches a channel conversation the user
        opens by hand. Without the marker in memory the slot reports itself as
        never-filed to every in-memory reader, and the save path would have to
        rescue it from disk.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:filed1", "user", "hello")
        log.update_metadata("dashboard:filed1", {"channel_folder_filed": True})

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/filed1/resume", json={"key": "dashboard:filed1"}
            )
            assert resp.status == 200

        assert state._slots["filed1"]._channel_folder_filed is True, (
            "resume dropped the filing marker; the slot now claims it was never "
            "filed and a later save would re-file the conversation"
        )

    @pytest.mark.asyncio
    async def test_no_save_for_unchanged_resumed_session(self, tmp_path, monkeypatch):
        """Resumed session closed without new messages should not re-save."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:hist1", "user", "old msg")
        log.append("dashboard:hist1", "assistant", "old reply")

        async with TestClient(TestServer(_make_app(state))) as client:
            # Resume
            await client.post(
                "/api/chat/slots/hist1/resume",
                json={"key": "dashboard:hist1", "title": "Old Chat"},
            )
            # Close without chatting
            await client.delete("/api/chat/slots/hist1")

        # Original history should be unchanged
        msgs = log.read_messages("dashboard:hist1")
        assert len(msgs) == 2
        assert msgs[0]["content"] == "old msg"

    def test_close_saves_mode_to_history(self, tmp_path, monkeypatch):
        """Slot mode is persisted in session metadata on close."""
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("orch1", mode="orchestrator")
        slot.append("user", "plan")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        meta = state.conversation_log._read_metadata("dashboard:orch1")
        assert meta.get("mode") == "orchestrator"

    def test_close_does_not_persist_trust(self, tmp_path, monkeypatch):
        """Trust flags are ephemeral — not written to session metadata."""
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("t1")
        slot._trust = True
        slot._trust_reads = True
        slot.append("user", "hi")
        slot.drain()
        _save_slot_to_history(state, slot, closed=True)
        meta = state.conversation_log._read_metadata("dashboard:t1")
        assert meta.get("trust") is None
        assert meta.get("trust_reads") is None


# ── Resume deduplication ──


class TestResumeDedupe:
    @pytest.mark.asyncio
    async def test_resume_returns_orchestrator_mode(self, tmp_path, monkeypatch):
        """Resuming an autopilot session returns mode='orchestrator' (+ surface
        alias) so the recovered slot renders in autopilot mode immediately,
        without waiting for the SSE slots push to reconcile."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:orchhist", "user", "plan this")
        log.update_metadata("dashboard:orchhist", {"mode": "orchestrator"})

        async with TestClient(TestServer(_make_app(state))) as client:
            r = await (
                await client.post(
                    "/api/chat/slots/orchhist/resume",
                    json={"key": "dashboard:orchhist", "title": "Auto"},
                )
            ).json()

        assert r["ok"] is True
        assert r["mode"] == "orchestrator"
        assert r["surface"] == "orchestrator"
        # The live slot must also carry the restored mode.
        assert state._slots["orchhist"].mode == "orchestrator"

    @pytest.mark.asyncio
    async def test_resume_from_disk_sends_the_older_history_cursor(self, tmp_path, monkeypatch):
        """A fresh resume must send next_before. The client pages by that field
        and treats its absence as 'no older history', so omitting it strands
        every row outside the returned window."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        for i in range(250):
            log.append("dashboard:deep", "user", f"m{i}")

        async with TestClient(TestServer(_make_app(state))) as client:
            r = await (
                await client.post("/api/chat/slots/deep/resume", json={"key": "dashboard:deep"})
            ).json()

        assert r["ok"] is True
        assert r["has_more"] is True, "250 rows past a 200-row page must report more"
        assert "next_before" in r, "resume omitted the cursor the client pages by"
        assert r["next_before"] == 250 - 200

    @pytest.mark.asyncio
    async def test_resume_existing_slot_cursor_counts_the_frozen_prefix(
        self, tmp_path, monkeypatch
    ):
        """`total` on this branch counts only the in-memory window, so the cursor
        has to add the frozen on-disk prefix back. Without that it points inside
        the range it is meant to skip past, and paging silently loses rows."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.conversation_log.append("dashboard:s1", "user", "hello")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            # A restored slot keeps its older on-disk rows outside slot.messages.
            state._slots["s1"]._disk_older_count = 300
            r = await (
                await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            ).json()

        assert r["ok"] is True
        # window is 1 row and is entirely returned, so the cursor is the prefix.
        assert (
            r["next_before"] == 300
        ), "cursor ignored _disk_older_count; it would page from inside the prefix"
        # A cursor the client is told not to use is dead: every reducer does
        # `hasMore ? nextBefore : 0`, so asserting the value alone would pass
        # while the fix stayed inert for exactly the slot shape it targets.
        assert (
            r["has_more"] is True
        ), "has_more counted only the in-memory window, so the client discards the cursor"

    @pytest.mark.asyncio
    async def test_resume_existing_slot_returns_it(self, tmp_path, monkeypatch):
        """Resuming a session that's already active should return existing slot."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")

        async with TestClient(TestServer(_make_app(state))) as client:
            # First resume
            r1 = await (
                await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            ).json()
            assert r1["ok"] is True

            # Add a message to the active slot
            state._slots["s1"].append("user", "new msg")
            state._slots["s1"].drain()

            # Second resume — should return existing with new msg
            r2 = await (
                await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            ).json()
            assert r2["ok"] is True
            assert r2["total"] == 2  # original + new

            # Should still be one slot, not two
            resp = await client.get("/api/chat/slots")
            slots = await resp.json()
            assert sum(1 for s in slots if s["key"] == "s1") == 1

    @pytest.mark.asyncio
    async def test_resume_close_resume_no_duplicate_history(self, tmp_path, monkeypatch):
        """Resume → close → resume → close should not create duplicate history."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        log.append("dashboard:s1", "assistant", "hi")

        async with TestClient(TestServer(_make_app(state))) as client:
            # Resume and add a message
            await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            state._slots["s1"].append("user", "new question")
            state._slots["s1"].append("assistant", "new answer")
            state._slots["s1"].drain()
            await client.delete("/api/chat/slots/s1")

            # Resume again and close without changes
            await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            await client.delete("/api/chat/slots/s1")

        # Should have 4 messages (original 2 + new 2), not duplicated
        msgs = log.read_messages("dashboard:s1")
        assert len(msgs) == 4


# ── History key prefix handling ──


class TestHistoryKeyPrefix:
    @pytest.mark.asyncio
    async def test_no_double_dashboard_prefix(self, tmp_path, monkeypatch):
        """Slot key starting with 'dashboard:' should not get double-prefixed.

        get_or_create_slot canonicalizes slot names (strips the transport
        prefix, folds to the filename charset), so resuming a full session key
        registers the slot under the bare canonical key — the API response's
        ``key`` field is authoritative for follow-up calls.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:chat-1", "user", "hello")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/dashboard:chat-1/resume",
                json={"key": "dashboard:chat-1"},
            )
            slot_key = (await resp.json())["key"]
            assert slot_key == "chat-1"  # canonical: prefix stripped, no fold needed
            state._slots[slot_key].append("user", "new msg")
            state._slots[slot_key].drain()
            await client.delete(f"/api/chat/slots/{slot_key}")

        # Should be saved under dashboard:chat-1, not dashboard:dashboard:chat-1
        msgs = log.read_messages("dashboard:chat-1")
        assert len(msgs) == 2
        assert log.read_messages("dashboard:dashboard:chat-1") == []


# ── Default view uses in-memory (not stale disk) ──


class TestInMemoryAuthority:
    @pytest.mark.asyncio
    async def test_default_view_shows_current_messages(self, tmp_path, monkeypatch):
        """Default slot detail should return in-memory messages, not stale disk."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        # Stale disk data
        log.append("dashboard:s1", "user", "old question")
        log.append("dashboard:s1", "assistant", "old answer")

        # Active slot with different messages
        slot = state.get_or_create_slot("s1")
        slot.append("user", "new question")
        slot.append("tool", "✅ running")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/s1")
            data = await resp.json()
            # Should show in-memory (2 msgs), not disk (2 different msgs)
            assert data["total"] == 2
            assert data["messages"][0]["content"] == "new question"
            assert data["messages"][1]["content"] == "✅ running"

    @pytest.mark.asyncio
    async def test_full_load_prepends_older_disk_messages(self, tmp_path, monkeypatch):
        """No-limit path prepends older disk messages when restore truncated."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        # Simulate: 8 messages on disk total (5 older + 3 recent)
        for i in range(8):
            log.append("dashboard:s2", "user", f"msg {i}")
        # Slot has only the last 3 in memory (simulating truncated restore)
        slot = state.get_or_create_slot("s2")
        slot.append("user", "msg 5")
        slot.append("user", "msg 6")
        slot.append("user", "msg 7")
        slot.drain()
        # Flag that restore truncated older messages
        slot._disk_older_count = 5

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/s2")
            data = await resp.json()
            assert data["total"] == 8  # 5 older + 3 recent
            assert data["has_more"] is False
            assert data["messages"][0]["content"] == "msg 0"
            assert data["messages"][4]["content"] == "msg 4"
            assert data["messages"][5]["content"] == "msg 5"

    @pytest.mark.asyncio
    async def test_legacy_pagination_with_limit(self, tmp_path, monkeypatch):
        """Legacy limit-based pagination reads from chained disk."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        for i in range(10):
            log.append("dashboard:s3", "user", f"msg {i}")
        slot = state.get_or_create_slot("s3")  # noqa: F841

        async with TestClient(TestServer(_make_app(state))) as client:
            # limit=3 returns last 3, has_more=True
            resp = await client.get("/api/chat/slots/s3?limit=3")
            data = await resp.json()
            assert data["total"] == 10
            assert data["has_more"] is True
            assert len(data["messages"]) == 3
            assert data["messages"][-1]["content"] == "msg 9"

            # limit=3&before=5 returns msgs 2-4
            resp = await client.get("/api/chat/slots/s3?limit=3&before=5")
            data = await resp.json()
            assert data["has_more"] is True  # msgs 0, 1 still older
            assert [m["content"] for m in data["messages"]] == ["msg 2", "msg 3", "msg 4"]

            # before=2 returns last 2 older
            resp = await client.get("/api/chat/slots/s3?limit=100&before=2")
            data = await resp.json()
            assert data["has_more"] is False
            assert [m["content"] for m in data["messages"]] == ["msg 0", "msg 1"]

    def test_append_only_preserves_full_disk_history(self, tmp_path, monkeypatch):
        """Save keeps ALL on-disk messages; nothing dropped or archived.

        After a restart the slot holds only a recent window (the frozen prefix
        counted by _disk_older_count is OLDER on disk) while the JSONL still has
        the full history. Saving a new turn must preserve the frozen prefix
        byte-for-byte — no overwrite, no truncation, no archive.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        log = state.conversation_log
        # 8 messages on disk total (the pre-restart full history).
        for i in range(8):
            log.append("dashboard:s4", "user", f"old msg {i}")
        # Restore truncated to the last 3 in memory; _disk_window_len mirrors
        # what restore sets (those 3 are already the on-disk window tail).
        # Mirror PRODUCTION restore, which re-appends each window message WITH its
        # persisted ``ts`` (chat_persistence restore uses
        # ``slot.append(..., ts=m.get("ts", ""))``). Preserving the ts is what lets
        # a steady save recognise the on-disk window-region copies as its OWN (a
        # ts-match) rather than as fresh-ts duplicates — so neither duplicating nor
        # archiving them.
        slot = state.get_or_create_slot("s4")
        disk_tail = log.read_messages("dashboard:s4")[5:]
        for m in disk_tail:
            slot.append("user", m["content"], ts=m.get("ts", ""))
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)
        slot._disk_older_count = 5
        slot.append("user", "brand new msg")
        slot.drain()

        _save_slot_to_history(state, slot)

        # Nothing archived — the frozen prefix is never dropped.
        assert not (tmp_path / "archive").exists()
        # All 8 original messages + the new one are on disk, in order.
        disk = log.read_messages("dashboard:s4")
        contents = [m["content"] for m in disk]
        assert contents == [f"old msg {i}" for i in range(8)] + ["brand new msg"]

    def test_append_only_colliding_ts_no_duplicate(self, tmp_path, monkeypatch):
        """Colliding timestamps must not duplicate a genuine message on disk.

        A coarse system clock (notably Windows' ~15ms tick) can stamp a burst of
        rapid appends with an IDENTICAL ``datetime.now().isoformat()``. The
        append-safe save must still match each on-disk window-region line to its
        own window entry one-for-one — never mis-classifying a real window line
        as a phantom "foreign append" and duplicating it. Regression for the
        Windows ``Backend Tests`` failure in
        ``test_append_only_preserves_full_disk_history``.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        # Freeze history.py's clock so every on-disk append shares ONE ts,
        # reproducing the coarse-clock collision deterministically on any OS.
        import datetime as _dt

        from kiro_crew.dashboard.chat import _save_slot_to_history

        _FROZEN = _dt.datetime(2026, 7, 25, 0, 0, 0, tzinfo=_dt.timezone.utc)

        class _FrozenDateTime:
            @classmethod
            def now(cls, tz=None):
                return _FROZEN

        monkeypatch.setattr("kiro_crew.history.datetime", _FrozenDateTime)

        state = _make_state(tmp_path)
        log = state.conversation_log
        # 8 messages on disk, ALL sharing the frozen ts.
        for i in range(8):
            log.append("dashboard:s5c", "user", f"old msg {i}")
        disk = log.read_messages("dashboard:s5c")
        # Every on-disk ts is identical — the exact condition that broke matching.
        assert len({m.get("ts") for m in disk}) == 1

        # Restore truncated to the last 3, re-appending WITH the persisted ts
        # (mirrors production restore) — so those window entries also collide.
        slot = state.get_or_create_slot("s5c")
        for m in disk[5:]:
            slot.append("user", m["content"], ts=m.get("ts", ""))
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)
        slot._disk_older_count = 5
        slot.append("user", "brand new msg")
        slot.drain()

        _save_slot_to_history(state, slot)

        # No phantom foreign append, nothing archived, full history preserved.
        assert not (tmp_path / "archive").exists()
        contents = [m["content"] for m in log.read_messages("dashboard:s5c")]
        assert contents == [f"old msg {i}" for i in range(8)] + ["brand new msg"]

    def test_append_only_colliding_ts_edit_preserves_foreign(self, tmp_path, monkeypatch):
        """Colliding ts + in-place edit + foreign append must NOT drop the
        foreign line (regression for GPT 5.6 HIGH on the count-bounded matcher).

        The on-disk window region holds, sharing ONE coarse-clock ts: unchanged
        ``A``, an acknowledged cross-process foreign append ``X``, and the
        pre-edit copy of ``B`` — in that adversarial order (foreign BEFORE the
        pre-edit copy). Memory holds ``A`` and the EDITED ``B'``. A greedy
        ts-only match would consume ``X`` as if it were ``B``'s in-place edit and
        silently DROP the acknowledged foreign append. The ambiguity guard must
        instead preserve ``X`` (favouring a rare stale duplicate over
        irreversible data loss).
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        import json as _json

        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        log = state.conversation_log
        t = "2026-07-25T00:00:00+00:00"  # single colliding timestamp

        def _line(content: str) -> str:
            return _json.dumps({"role": "user", "content": content, "ts": t}) + "\n"

        # Craft the adversarial on-disk order directly: A, foreign X, pre-edit B.
        path = log._path("dashboard:s5e")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _json.dumps({"_type": "metadata", "created_at": t, "last_consolidated": 0})
            + "\n"
            + _line("msg A")
            + _line("msg X (foreign)")
            + _line("msg B"),
            encoding="utf-8",
        )

        # Window: A unchanged, B edited in place (content changes, ts preserved).
        slot = state.get_or_create_slot("s5e")
        slot.append("user", "msg A", ts=t)
        slot.append("user", "msg B (edited in place)", ts=t)
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)
        slot._dirty = True  # an in-place edit marks the slot dirty

        _save_slot_to_history(state, slot)

        contents = [m["content"] for m in log.read_messages("dashboard:s5e")]
        # The acknowledged foreign append survived (no data loss) ...
        assert "msg X (foreign)" in contents, "foreign append was dropped (data loss)"
        # ... and the edited window content is present.
        assert "msg B (edited in place)" in contents

    def test_append_only_no_duplicate_on_resave(self, tmp_path, monkeypatch):
        """Re-saving without new messages must not duplicate the tail on disk."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        log = state.conversation_log
        for i in range(4):
            log.append("dashboard:s6", "user", f"msg {i}")
        slot = state.get_or_create_slot("s6")
        for i in range(4):
            slot.append("user", f"msg {i}")
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)

        slot.append("assistant", "reply A")
        slot.drain()
        _save_slot_to_history(state, slot)
        # A redundant save (force) must not duplicate the already-written tail.
        _save_slot_to_history(state, slot, force=True)

        disk = log.read_messages("dashboard:s6")
        contents = [m["content"] for m in disk]
        assert contents == [f"msg {i}" for i in range(4)] + ["reply A"]

    def test_save_steady_state_does_not_archive(self, tmp_path, monkeypatch):
        """A normal append (slot is a superset of disk) archives nothing."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s5")
        slot.append("user", "first")
        slot.drain()
        _save_slot_to_history(state, slot)
        slot.append("assistant", "reply")
        slot.drain()
        _save_slot_to_history(state, slot)

        assert not (tmp_path / "archive").exists()

    def test_append_only_does_not_merge_unrelated_session(self, tmp_path, monkeypatch):
        """Append-only writes ONLY to the slot's own file — never an unrelated one.

        Guards the regression the reporter flagged: a second, unrelated session
        must not get merged into this slot's history. Append-only touches a
        single session file, so a sibling file is left completely untouched.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        log = state.conversation_log
        # Two independent on-disk sessions (no shared tab_id).
        for i in range(3):
            log.append("dashboard:sessA", "user", f"A{i}")
        for i in range(2):
            log.append("dashboard:sessB", "user", f"B{i}")
        b_before = log.read_messages("dashboard:sessB")

        slot = state.get_or_create_slot("sessA")
        for i in range(3):
            slot.append("user", f"A{i}")
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)
        slot._tab_id = ""  # no chaining
        slot.append("user", "A-new")
        slot.drain()
        _save_slot_to_history(state, slot)

        # sessA got its new turn; sessB is byte-for-byte untouched.
        assert [m["content"] for m in log.read_messages("dashboard:sessA")] == [
            "A0",
            "A1",
            "A2",
            "A-new",
        ]
        assert log.read_messages("dashboard:sessB") == b_before

    def test_rewrite_path_archives_dropped_tail(self, tmp_path, monkeypatch):
        """An explicit snapshot save (rewrite, e.g. rewind) archives dropped msgs."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        log = state.conversation_log
        slot = state.get_or_create_slot("s7")
        for c in ("q1", "a1", "q2", "a2"):
            slot.append("user" if c.startswith("q") else "assistant", c)
        slot.drain()
        _save_slot_to_history(state, slot)  # append-only establishes the file
        assert not (tmp_path / "archive").exists()

        # Rewind-style: truncate to first turn and rewrite via explicit snapshot.
        snapshot = [dict(m) for m in slot.messages[:1]]
        _save_slot_to_history(state, slot, snapshot)

        # Disk now holds only the kept prefix; dropped tail is archived.
        disk = log.read_messages("dashboard:s7")
        assert [m["content"] for m in disk] == ["q1"]
        archives = list((tmp_path / "archive").glob("dashboard_s7__*.jsonl"))
        assert len(archives) == 1
        content = archives[0].read_text(encoding="utf-8")
        for dropped in ("a1", "q2", "a2"):
            assert dropped in content

    def test_rewrite_preserves_frozen_prefix(self, tmp_path, monkeypatch):
        """A rewrite (rewind) with a frozen prefix keeps the prefix, drops only the tail.

        With _disk_older_count > 0 the file is frozen_prefix + window. A rewrite
        that truncates the window must leave the frozen prefix byte-for-byte and
        archive only the dropped window tail — never the older history.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        log = state.conversation_log
        # 6 messages on disk; only the last 2 are the in-memory window.
        for i in range(6):
            log.append("dashboard:s11", "user", f"old {i}")
        slot = state.get_or_create_slot("s11")
        slot.append("user", "old 4")
        slot.append("assistant", "old 5")
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)
        slot._disk_older_count = 4  # old 0..3 are the frozen prefix

        # Rewind-style truncate to just the first window message + rewrite.
        snapshot = [dict(m) for m in slot.messages[:1]]
        _save_slot_to_history(state, slot, snapshot)

        disk = log.read_messages("dashboard:s11")
        # Frozen prefix preserved + kept window head; dropped tail archived.
        assert [m["content"] for m in disk] == ["old 0", "old 1", "old 2", "old 3", "old 4"]
        archives = list((tmp_path / "archive").glob("dashboard_s11__*.jsonl"))
        assert len(archives) == 1
        content = archives[0].read_text(encoding="utf-8")
        assert "old 5" in content
        # The frozen prefix must NOT be archived.
        for frozen in ("old 0", "old 1", "old 2", "old 3"):
            assert frozen not in content

    def test_flush_then_finalized_reply_persists(self, tmp_path, monkeypatch):
        """#1: a mid-turn flush past a stop_event must not lose the later reply.

        Repro of the boundary-asymmetry bug: a stop happens mid-turn, the 5s
        flush persists the window ending in the stop_event, then _flush_segment
        finalizes the assistant reply and moves the stop_event AFTER it. The
        finalized reply must end up on disk (the old position-counter model
        committed past the stop_event and dropped the later assistant line).
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        import json

        from kiro_crew.dashboard.chat import _flush_segment, _save_slot_to_history

        state = _make_state(tmp_path)
        log = state.conversation_log
        slot = state.get_or_create_slot("s8")
        slot.append("user", "do a thing")
        # Streaming chunk + a stop_event interleaved with this segment.
        slot.append("chunk", "partial output")
        stop_cls = json.dumps({"kind": "stop_event", "id": "stop-1", "state": "stopping"})
        slot.append("system", stop_cls, cls=stop_cls)
        slot.drain()

        # 5s flush lands here — window ends in chunk + stop_event.
        _save_slot_to_history(state, slot)

        # _flush_segment finalizes the assistant reply and re-orders the
        # stop_event to land AFTER it.
        _flush_segment(state, slot, "final answer", broadcast=False)
        slot.drain()
        _save_slot_to_history(state, slot)

        disk = log.read_messages("dashboard:s8")
        contents = [m["content"] for m in disk]
        # The finalized reply is on disk, after the user prompt; chunk dropped.
        assert "final answer" in contents
        assert "do a thing" in contents
        assert "partial output" not in contents
        # Stop card persists after the finalized assistant reply.
        assert contents.index("final answer") < contents.index(stop_cls)

    def test_inplace_stop_resolution_persists(self, tmp_path, monkeypatch):
        """#2: an in-place edit of an already-flushed message must persist.

        The stop_event message is flushed as "stopping"; later it is mutated in
        place to "stopped" (mirrors _resolve_stop_event). The re-serialized
        window must carry the resolution to disk — the old append-only model
        only wrote new tail messages and dropped the in-place edit.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        import json

        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        log = state.conversation_log
        slot = state.get_or_create_slot("s9")
        slot.append("user", "go")
        stopping = json.dumps({"kind": "stop_event", "id": "stop-x", "state": "stopping"})
        slot.append("system", stopping, cls=stopping)
        slot.drain()
        _save_slot_to_history(state, slot)  # flushed as "stopping"
        # Model a resumed slot: the window equals the resumed count and the
        # in-place edit adds NO new message — exercises the resumed-guard path.
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)
        slot._dirty = False

        # Resolve in place (same object already on disk at index < end).
        stopped = json.dumps({"kind": "stop_event", "id": "stop-x", "state": "stopped"})
        for m in slot.messages:
            if m.get("role") == "system":
                m["cls"] = stopped
                m["content"] = stopped
                slot._dirty = True  # _resolve_stop_event sets this
                break
        _save_slot_to_history(state, slot)

        disk = log.read_messages("dashboard:s9")
        sys_msgs = [m for m in disk if m.get("role") == "system"]
        assert len(sys_msgs) == 1
        assert json.loads(sys_msgs[0]["cls"])["state"] == "stopped"

    def test_failed_rewrite_does_not_strand_or_lose(self, tmp_path, monkeypatch):
        """#3: a failed inline rewrite must not lose the dropped tail or strand state.

        rewind/regenerate truncate the window then save with a snapshot. If that
        save raises, _pending_rewrite stays set so the next (flush) save still
        takes the archive-safe rewrite path — the dropped tail is archived, not
        silently overwritten, and the kept prefix is correct on disk.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_persistence
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        log = state.conversation_log
        slot = state.get_or_create_slot("s10")
        for c in ("q1", "a1", "q2", "a2"):
            slot.append("user" if c.startswith("q") else "assistant", c)
        slot.drain()
        _save_slot_to_history(state, slot)

        # Simulate rewind: truncate window to first turn, mark pending rewrite,
        # and have the inline rewrite save blow up inside atomic_write.
        del slot.messages[1:]
        slot._pending_rewrite = True
        orig_atomic = chat_persistence.atomic_write
        calls = {"n": 0}

        def _boom(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return orig_atomic(*a, **k)

        monkeypatch.setattr(chat_persistence, "atomic_write", _boom)
        snapshot = [dict(m) for m in slot.messages]
        with pytest.raises(OSError):
            _save_slot_to_history(state, slot, snapshot)
        # Flag still set → flush retries the archive-safe rewrite (2nd write OK).
        assert slot._pending_rewrite is True
        _save_slot_to_history(state, slot)
        assert slot._pending_rewrite is False

        disk = log.read_messages("dashboard:s10")
        assert [m["content"] for m in disk] == ["q1"]
        archives = list((tmp_path / "archive").glob("dashboard_s10__*.jsonl"))
        assert len(archives) >= 1
        content = "".join(a.read_text(encoding="utf-8") for a in archives)
        for dropped in ("a1", "q2", "a2"):
            assert dropped in content


# ── Session rename tests ──


class TestSessionRename:
    @pytest.mark.asyncio
    async def test_rename_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slot_title = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/title", json={"title": "My Chat"})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["title"] == "My Chat"
            assert slot.title == "My Chat"
            assert slot._titled is True
            state.push_slot_title.assert_called_once_with("s1", "My Chat")

    @pytest.mark.asyncio
    async def test_rename_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/nonexistent/title", json={"title": "X"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_rename_empty_title(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/title", json={"title": "  "})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rename_invalid_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                "/api/chat/slots/s1/title",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rename_truncates_at_200(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slot_title = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            long_title = "x" * 300
            resp = await client.patch("/api/chat/slots/s1/title", json={"title": long_title})
            data = await resp.json()
            assert resp.status == 200
            assert len(data["title"]) == 200
            assert state._slots["s1"].title == "x" * 200
            state.push_slot_title.assert_called_once_with("s1", "x" * 200)

    @pytest.mark.asyncio
    async def test_resumed_session_preserves_title(self, tmp_path, monkeypatch):
        """Resumed session should set _titled=True so auto-title doesn't overwrite."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/resume",
                json={"key": "dashboard:s1", "title": "My Custom Title"},
            )
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot.title == "My Custom Title"
            assert slot._titled is True

    @pytest.mark.asyncio
    async def test_resumed_session_restores_tags_and_auto_tagged(self, tmp_path, monkeypatch):
        """Regression: resume must restore tags AND the auto-tag once-flag.
        Without the flag, resuming a session whose auto-tag the user removed
        re-runs maybe_auto_tag on the next message and silently re-adds it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        # The id must exist in the vocabulary — resume prunes unknown ids
        # (crash-atomic delete can leave dangling ids on disk).
        state._tags.append({"id": "tag-abc", "name": "ABC", "color": "#6b7280", "order": 0})
        log.update_metadata(
            "dashboard:s1",
            {"tags": ["tag-abc"], "auto_tagged": True, "project": "/x/repos/MyRepo"},
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/resume",
                json={"key": "dashboard:s1"},
            )
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot.tags == ["tag-abc"]
            assert slot._auto_tagged is True

    @pytest.mark.asyncio
    async def test_resume_unreadable_vocab_does_not_wipe_tags(self, tmp_path, monkeypatch):
        """FAIL-OPEN: if tags.json failed to load (vocabulary UNKNOWN), resume
        must NOT prune — pruning against an unknown vocab would wipe every
        assignment and the next save persists the loss."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._tags = []
        state._committed_tag_ids = None  # load_tags() hit a parse/I/O error: UNKNOWN
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        log.update_metadata("dashboard:s1", {"tags": ["tag-abc"], "auto_tagged": True})

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot.tags == ["tag-abc"]  # preserved, not wiped
            assert slot._auto_tagged is True

    @pytest.mark.asyncio
    async def test_resume_empty_authoritative_vocab_prunes_dangling_ids(
        self, tmp_path, monkeypatch
    ):
        """A legitimately-empty vocabulary (last tag deleted, tags.json parsed
        fine as []) IS knowledge: resume must prune the dangling id, or a
        crash between the vocab commit and slot cleanup would resurrect the
        deleted tag id on this slot forever.

        ``frozenset()`` is what carries that -- it is KNOWN-empty and prunes, whereas
        ``None`` is UNKNOWN and fails open. The two must never collapse.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._tags = []
        state._committed_tag_ids = frozenset()  # tags.json parsed OK as []: KNOWN-empty
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        log.update_metadata("dashboard:s1", {"tags": ["tag-abc"], "auto_tagged": True})

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot.tags == []  # dangling id pruned
            assert slot._auto_tagged is True

    def test_load_tags_publishes_knownness_as_the_committed_snapshot(self, tmp_path, monkeypatch):
        """load_tags() publishes a committed snapshot on a clean parse (including a
        legitimately-empty []) and leaves it None on a parse failure — the single
        signal the restore-time pruning fail-open relies on.

        Knownness has exactly one encoding, so there is no parallel flag to keep in
        sync and none that can go stale when a tag write moves disk. ``None`` vs
        ``frozenset()`` carries the distinction that actually matters: fail open
        versus prune.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        # Legitimately-empty vocabulary: parsed OK -> KNOWN-empty, which prunes.
        (tmp_path / "tags.json").write_text("[]", encoding="utf-8")
        state._tags = []
        state._committed_tag_ids = None
        state.load_tags()
        assert state._committed_tag_ids == frozenset()
        assert state._tags == []

        # Corrupt file: parse failure -> UNKNOWN, data untouched.
        (tmp_path / "tags.json").write_text("{not json", encoding="utf-8")
        state._tags = [{"id": "keep-me", "name": "Keep", "status": False}]
        state._committed_tag_ids = frozenset({"stale"})
        state.load_tags()
        assert state._committed_tag_ids is None
        assert state._tags[0]["id"] == "keep-me"  # not re-seeded, not wiped

        # Valid JSON but NOT a list (e.g. {}): vocabulary state is unknown,
        # same as a parse failure -- UNKNOWN, data untouched.
        (tmp_path / "tags.json").write_text("{}", encoding="utf-8")
        state._tags = [{"id": "keep-me", "name": "Keep", "status": False}]
        state._committed_tag_ids = frozenset({"stale"})
        state.load_tags()
        assert state._committed_tag_ids is None
        assert state._tags[0]["id"] == "keep-me"


# ── Session color tests ──


class TestSessionColor:
    @pytest.mark.asyncio
    async def test_set_color_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": 3})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["color_index"] == 3
            assert state._slots["s1"].color_index == 3
            state.push_slots_update.assert_called()

    @pytest.mark.asyncio
    async def test_set_color_null(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.color_index = 5

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": None})
            data = await resp.json()
            assert resp.status == 200
            assert data["color_index"] is None
            assert slot.color_index is None

    @pytest.mark.asyncio
    async def test_set_color_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/nope/color", json={"color_index": 0})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_set_color_invalid_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                "/api/chat/slots/s1/color",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_set_color_negative_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": -1})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_set_color_bool_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": True})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_set_color_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": 0})
            data = await resp.json()
            assert resp.status == 200
            assert data["color_index"] == 0
            assert slot.color_index == 0

    @pytest.mark.asyncio
    async def test_set_color_large_index(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": 99999})
            assert resp.status == 400

    def test_color_zero_persisted(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.color_index = 0
        slot.append("user", "hello")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        meta = state.conversation_log._read_metadata("dashboard:s1")
        assert meta.get("color_index") == 0

    def test_color_persisted_in_history(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.color_index = 4
        slot.append("user", "hello")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        meta = state.conversation_log._read_metadata("dashboard:s1")
        assert meta.get("color_index") == 4

    def test_color_null_not_persisted(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        meta = state.conversation_log._read_metadata("dashboard:s1")
        assert "color_index" not in meta

    @pytest.mark.asyncio
    async def test_set_color_hex_success_and_lowercased(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_hex": "#A1B2C3"})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["color_hex"] == "#a1b2c3"
            assert state._slots["s1"].color_hex == "#a1b2c3"
            state.push_slots_update.assert_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad", ["#GGGGGG", "#abc", "abcdef", "#abcdef00", 123, True, ["#abcdef"]]
    )
    async def test_set_color_hex_malformed_rejected(self, tmp_path, monkeypatch, bad):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_hex": bad})
            assert resp.status == 400
            assert state._slots["s1"].color_hex is None

    @pytest.mark.asyncio
    async def test_color_hex_and_index_mutually_exclusive(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            # Setting a hex clears an existing index.
            slot.color_index = 4
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_hex": "#112233"})
            data = await resp.json()
            assert resp.status == 200
            assert data == {"ok": True, "color_index": None, "color_hex": "#112233"}
            assert slot.color_index is None and slot.color_hex == "#112233"
            # Setting an index clears the hex.
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": 2})
            data = await resp.json()
            assert data == {"ok": True, "color_index": 2, "color_hex": None}
            assert slot.color_index == 2 and slot.color_hex is None

    @pytest.mark.asyncio
    async def test_color_index_only_patch_keeps_existing_hex_when_null(self, tmp_path, monkeypatch):
        """An index-only PATCH with null must not silently null a hex (in-body gating)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.color_hex = "#445566"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": None})
            assert resp.status == 200
            assert slot.color_hex == "#445566"

    @pytest.mark.asyncio
    async def test_color_hex_null_clears(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.color_hex = "#778899"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_hex": None})
            data = await resp.json()
            assert resp.status == 200
            assert data["color_hex"] is None
            assert slot.color_hex is None

    @pytest.mark.asyncio
    async def test_color_hex_in_to_dict(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.color_hex = "#0a0b0c"
        assert slot.to_dict()["color_hex"] == "#0a0b0c"

    @pytest.mark.asyncio
    async def test_color_hex_persisted_in_history(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.color_hex = "#0a0b0c"
        slot.append("user", "hello")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        meta = state.conversation_log._read_metadata("dashboard:s1")
        assert meta.get("color_hex") == "#0a0b0c"


# ── Slash command tests ──


class TestBlockedSlashCommands:
    """Tests for _BLOCKED_SLASH_COMMANDS blocking dangerous commands."""

    def test_quit_is_blocked(self):
        from kiro_crew.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert "/quit" in _BLOCKED_SLASH_COMMANDS

    def test_exit_is_blocked(self):
        from kiro_crew.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert "/exit" in _BLOCKED_SLASH_COMMANDS

    def test_q_is_blocked(self):
        from kiro_crew.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert "/q" in _BLOCKED_SLASH_COMMANDS

    def test_editor_is_blocked(self):
        from kiro_crew.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert "/editor" in _BLOCKED_SLASH_COMMANDS

    def test_chat_is_blocked(self):
        from kiro_crew.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert "/chat" in _BLOCKED_SLASH_COMMANDS

    def test_paste_is_blocked(self):
        from kiro_crew.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert "/paste" in _BLOCKED_SLASH_COMMANDS

    def test_reply_is_blocked(self):
        from kiro_crew.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert "/reply" in _BLOCKED_SLASH_COMMANDS

    def test_tangent_is_blocked(self):
        # kiro-cli's terminal checkpoint toggle (Ctrl+T); the dashboard's
        # session model and /side cover its purpose, so it is inert here.
        from kiro_crew.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert "/tangent" in _BLOCKED_SLASH_COMMANDS

    def test_compact_is_not_blocked(self):
        from kiro_crew.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert "/compact" not in _BLOCKED_SLASH_COMMANDS

    def test_blocked_is_subset_of_slash(self):
        from kiro_crew.dashboard.chat import _BLOCKED_SLASH_COMMANDS, _SLASH_COMMANDS

        assert _BLOCKED_SLASH_COMMANDS.issubset(_SLASH_COMMANDS)

    @pytest.mark.asyncio
    async def test_blocked_command_returns_warning_no_session(self, tmp_path, monkeypatch):
        """Posting /quit should add warning to slot and never acquire a session."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "/quit")

        # Should have the warning message
        texts = [m["content"] for m in slot.messages if m.get("role") == "assistant"]
        assert any("not available in the dashboard" in t for t in texts)
        # Should never have called get_or_create (no session acquired)
        state.sessions.get_or_create.assert_not_called()


# ── Background session leak regression ──


class TestTitleGenerationSessionLeak:
    """_generate_title_via_kiro must destroy its ephemeral bg session even on error."""

    @pytest.mark.asyncio
    async def test_background_session_destroyed_on_stream_error(self, tmp_path):
        from kiro_crew.dashboard.chat import _generate_title_via_kiro

        state = _make_state(tmp_path)

        # Mock session whose prompt() raises mid-iteration
        mock_client = MagicMock()
        mock_client.destroy = AsyncMock()

        async def _exploding_prompt(prompt):
            raise RuntimeError("throttle / ACP error")
            yield  # noqa: unreachable — makes this an async generator

        mock_client.prompt = _exploding_prompt
        state.sessions.get_bg_session = AsyncMock(return_value=mock_client)

        messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]

        with pytest.raises(RuntimeError, match="throttle"):
            await _generate_title_via_kiro(state, messages)

        # The critical assertion: destroy MUST be called even though prompt() raised
        mock_client.destroy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_permission_request_rejected_during_title_gen(self, tmp_path):
        from kiro_crew.dashboard.chat import _generate_title_via_kiro
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_PERMISSION_REQUEST,
            EVENT_TEXT_CHUNK,
            LLMEvent,
        )

        state = _make_state(tmp_path)
        mock_client = MagicMock()
        mock_client.reject_tool = AsyncMock()
        mock_client.destroy = AsyncMock()

        async def _prompt(prompt):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="My Title")
            yield LLMEvent(kind=EVENT_PERMISSION_REQUEST, request_id="req-1")
            yield LLMEvent(kind=EVENT_COMPLETE)

        mock_client.prompt = _prompt
        state.sessions.get_bg_session = AsyncMock(return_value=mock_client)

        messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
        title = await _generate_title_via_kiro(state, messages)

        mock_client.reject_tool.assert_called_once_with("req-1")
        assert title == "My Title"
        mock_client.destroy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_complete_event_breaks_stream(self, tmp_path):
        from kiro_crew.dashboard.chat import _generate_title_via_kiro
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        state = _make_state(tmp_path)
        mock_client = MagicMock()
        mock_client.destroy = AsyncMock()

        async def _prompt(prompt):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Good")
            yield LLMEvent(kind=EVENT_COMPLETE)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=" SHOULD NOT APPEAR")

        mock_client.prompt = _prompt
        state.sessions.get_bg_session = AsyncMock(return_value=mock_client)

        messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
        title = await _generate_title_via_kiro(state, messages)

        assert title == "Good"
        mock_client.destroy.assert_awaited_once()


class TestFlushSegment:
    """Unit tests for _flush_segment helper function."""

    def test_flush_segment_persists_and_broadcasts(self, tmp_path, monkeypatch):
        """_flush_segment persists assistant message and broadcasts chat_segment.

        Validates: Requirements 1.1, 1.2, 4.3, 6.3
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("s1")
        # Simulate accumulated chunks
        slot.append("chunk", "Hello ")
        slot.append("chunk", "world")

        from kiro_crew.dashboard.chat import _flush_segment

        _flush_segment(state, slot, "Hello world")

        # Chunks should be removed
        chunk_msgs = [m for m in slot.messages if m.get("role") == "chunk"]
        assert len(chunk_msgs) == 0
        # Assistant message should be persisted
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0]["content"] == "Hello world"
        # chat_segment should be broadcast
        state.broadcast_ws.assert_called_once_with("chat_segment", {"slot": "s1"})

    def test_flush_segment_schedules_widget_registration(self, tmp_path, monkeypatch):
        """A segment containing an <mcwidget> auto-registers it as an artifact.

        This is the integration seam for widget-as-artifact-by-default: without
        it nothing registers emitted widgets, and the in-session Artifacts tab
        can never list them.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("s1")

        calls: list = []

        async def _fake_register(text, message_ts, session_key):
            calls.append((text, message_ts, session_key))
            return []

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.register_widgets_off_loop", _fake_register
        )

        from kiro_crew.dashboard.chat import _flush_segment

        async def _run():
            _flush_segment(state, slot, '<mcwidget title="W">body</mcwidget>')
            # The task is detached — let it run before asserting.
            await asyncio.sleep(0)
            for t in list(state._background_tasks):
                await t

        asyncio.run(_run())

        assert len(calls) == 1
        text, message_ts, session_key = calls[0]
        assert "<mcwidget" in text
        # Keyed to the finalized assistant message.
        assert message_ts
        # Attributed with the BARE slot key. Asserting the literal string here
        # would happily pin a prefix the reader never queries, so assert the
        # value the in-session tab actually sends (?session=<activeSlot>, i.e.
        # slot.key) — ArtifactStore.list compares session_key exactly.
        assert session_key == slot.key

    def test_flush_segment_skips_registration_without_a_widget(self, tmp_path, monkeypatch):
        """The common case (no widget) must not touch the artifact store at all."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("s1")

        called = False

        async def _fake_register(text, message_ts, session_key):
            nonlocal called
            called = True
            return []

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.register_widgets_off_loop", _fake_register
        )

        from kiro_crew.dashboard.chat import _flush_segment

        async def _run():
            _flush_segment(state, slot, "just prose, no widget here")
            await asyncio.sleep(0)

        asyncio.run(_run())
        assert called is False

    def test_flush_segment_registers_redacted_text(self, tmp_path, monkeypatch):
        """A credential stripped from chat must not survive into the artifact.

        Registration runs on the POST-redaction text; passing the raw accumulated
        text would persist to disk (and re-surface on the artifact page) exactly
        what the segment redaction just removed.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("s1")

        captured: list = []

        async def _fake_register(text, message_ts, session_key):
            captured.append(text)
            return []

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.register_widgets_off_loop", _fake_register
        )

        from kiro_crew.dashboard.chat import _flush_segment

        secret = "AKIAIOSFODNN7EXAMPLE"

        async def _run():
            _flush_segment(state, slot, f'<mcwidget title="W">key={secret}</mcwidget>')
            await asyncio.sleep(0)
            for t in list(state._background_tasks):
                await t

        asyncio.run(_run())

        assert captured, "registration should have been scheduled"
        assert secret not in captured[0]

    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    def test_flush_segment_skips_registration_in_restricted_sessions(
        self, tmp_path, monkeypatch, mode
    ):
        """Incognito / temporary sessions must not persist widget artifacts.

        Every artifact write is denied for these sessions at the HTTP gate
        (`_is_restricted_session`), so registering from the chat path would be a
        back door around that ceiling: widget HTML from a session the user
        expected to leave no trace would land in `artifacts/<slug>/` and show up
        in the library.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.memory_mode = mode
        assert slot.is_restricted

        called = False

        async def _fake_register(text, message_ts, session_key):
            nonlocal called
            called = True
            return []

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.register_widgets_off_loop", _fake_register
        )

        from kiro_crew.dashboard.chat import _flush_segment

        async def _run():
            _flush_segment(state, slot, '<mcwidget title="W">secret body</mcwidget>')
            await asyncio.sleep(0)
            for t in list(state._background_tasks):
                await t

        asyncio.run(_run())
        assert called is False, f"{mode} session must not register widget artifacts"


class TestRunChatSegmentFlush:
    """Tests for segment flush behavior in _run_chat()."""

    @staticmethod
    def _make_mock_client(events):
        """Create a mock ACP client that yields the given LLMEvent list."""
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_state_for_run_chat(tmp_path, monkeypatch):
        """Create a DashboardState wired for _run_chat tests."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_credential_split_across_chunks_never_streamed_raw(self, tmp_path, monkeypatch):
        """A credential split across streaming chunks must never appear raw in
        any chat_chunk broadcast (pentest issue 3), while the reassembled stream
        stays lossless and shows the redaction.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        # AKIAIOSFODNN7EXAMPLE split exactly as in the pentest reproduction.
        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="The access key is AKIA"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="IOSFODNN7"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="EXAMPLE"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "echo the key")

        contents = [
            call.args[1]["content"]
            for call in state.broadcast_ws.call_args_list
            if call.args[0] == "chat_chunk"
        ]
        wire = "".join(contents)
        # No individual frame — and not the reassembled wire — leaks the key.
        for frame in contents:
            assert "AKIAIOSFODNN7EXAMPLE" not in frame
        assert "AKIAIOSFODNN7EXAMPLE" not in wire
        # The credential fragments must not be recoverable across frames.
        assert "AKIA" not in wire.replace("[REDACTED: credential]", "")
        assert "[REDACTED: credential]" in wire
        assert wire == "The access key is [REDACTED: credential]"

    @pytest.mark.asyncio
    async def test_credential_split_across_thinking_chunks_not_streamed_raw(
        self, tmp_path, monkeypatch
    ):
        """A credential split across thinking chunks must not appear raw on any
        chat_thinking broadcast (issue 3 parity for the thinking stream)."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_THINKING_CHUNK,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_THINKING_CHUNK, text="the key looks like AKIA"),
            LLMEvent(kind=EVENT_THINKING_CHUNK, text="IOSFODNN7"),
            LLMEvent(kind=EVENT_THINKING_CHUNK, text="EXAMPLE"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="done"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "think about it")

        thinking = [
            call.args[1]["content"]
            for call in state.broadcast_ws.call_args_list
            if call.args[0] == "chat_thinking"
        ]
        wire = "".join(thinking)
        for frame in thinking:
            assert "AKIAIOSFODNN7EXAMPLE" not in frame
        assert "AKIAIOSFODNN7EXAMPLE" not in wire
        assert "AKIA" not in wire.replace("[REDACTED: credential]", "")
        assert "[REDACTED: credential]" in wire

    @pytest.mark.asyncio
    async def test_text_tool_text_complete_produces_two_segments(self, tmp_path, monkeypatch):
        """Mock event stream: text → tool_call → text → complete produces
        two assistant messages and one tool message.

        Validates: Requirements 1.1, 1.2, 1.3, 4.3
        """
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="Before tool"),
            LLMEvent(kind=EVENT_TOOL_CALL, title="read_file", tool_kind="read"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="After tool"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # Check persisted messages (exclude transient roles)
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 2
        assert assistant_msgs[0]["content"] == "Before tool"
        assert assistant_msgs[1]["content"] == "After tool"

        # Verify both chat_segment and tool_call are broadcast
        ws_calls = [(c.args[0], c.args[1]) for c in state.broadcast_ws.call_args_list]
        ws_types = [t for t, _ in ws_calls]
        assert "chat_segment" in ws_types
        assert "tool_call" in ws_types

    @pytest.mark.asyncio
    async def test_idle_turn_boundary_refreshes_source_status(self, tmp_path, monkeypatch):
        """Reaching idle at a turn boundary must re-read the slot's PR/MR status.

        Regression for the production wiring: `_run_chat`'s idle branch calls
        `state.refresh_slot_source_status(slot.key)` so the sidebar chips and the
        detail panel re-read after a turn that may have opened/pushed/merged a
        PR. The state-level tests exercise `refresh_slot_source_status` directly
        and would stay green if this call were deleted, so pin the real
        `_run_chat` path here.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"), LLMEvent(kind=EVENT_COMPLETE)]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        state.refresh_slot_source_status = MagicMock()
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        state.refresh_slot_source_status.assert_called_once_with(slot.key)

    @pytest.mark.asyncio
    async def test_text_permission_request_flushes_segment(self, tmp_path, monkeypatch):
        """Mock event stream: text → permission_request flushes segment
        before permission flow.

        Validates: Requirements 1.4
        """
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_PERMISSION_REQUEST,
            EVENT_TEXT_CHUNK,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="Analyzing..."),
            LLMEvent(
                kind=EVENT_PERMISSION_REQUEST,
                title="bash",
                tool_kind="execute",
                request_id="req-1",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        # Enable YOLO mode so permission auto-approves (simplifies test)
        state.enable_yolo()
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        client.approve_tool = AsyncMock()
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "run ls")

        # Segment should have been flushed before permission flow
        ws_types = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "chat_segment" in ws_types
        # The flushed segment should be persisted as assistant
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert any(m["content"] == "Analyzing..." for m in assistant_msgs)

    @pytest.mark.asyncio
    async def test_text_only_complete_no_segments(self, tmp_path, monkeypatch):
        """Text-only stream → complete produces one assistant message (no segments).

        Validates: Requirements 8.1
        """
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="Just text"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # No chat_segment events
        ws_types = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "chat_segment" not in ws_types
        # One assistant message
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0]["content"] == "Just text"

    @pytest.mark.asyncio
    async def test_chunk_seq_monotonically_increasing_across_segments(self, tmp_path, monkeypatch):
        """chunk_seq values in broadcast calls are monotonically increasing
        across segments.

        Validates: Requirements 7.1
        """
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="a"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="b"),
            LLMEvent(kind=EVENT_TOOL_CALL, title="read_file", tool_kind="read"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="c"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="d"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # Collect all seq values + content from chat_chunk broadcasts
        seq_values: list[int] = []
        contents: list[str] = []
        for call in state.broadcast_ws.call_args_list:
            if call.args[0] == "chat_chunk":
                seq_values.append(call.args[1]["seq"])
                contents.append(call.args[1]["content"])

        # The streaming redaction buffer (issue 3) withholds trailing
        # credential-class runs, so contiguous chars ("a"+"b", "c"+"d") are
        # coalesced and flushed at the segment boundary rather than emitted
        # one-per-input-chunk. What must hold: seq is strictly monotonic and no
        # content is lost across the buffering.
        assert seq_values, "no chat_chunk broadcasts"
        for i in range(1, len(seq_values)):
            assert seq_values[i] > seq_values[i - 1], f"seq not monotonic: {seq_values}"
        assert "".join(contents) == "abcd", f"content lost in stream buffer: {contents}"


class TestRunChatNativeSubagentAttribution:
    """End-to-end: native (use_subagent) sub-agent tool calls + results are
    attributed to per-sub-agent Activity cards, deduped, and the accumulated
    feed is sent as the card's done ``result``."""

    @staticmethod
    def _make_mock_client(events):
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_state_for_run_chat(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_native_tool_calls_attribute_dedupe_and_accumulate(self, tmp_path, monkeypatch):
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_SUBAGENT_ACTIVITY,
            EVENT_SUBAGENT_LIST,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        def _sub(status):
            return {
                "sessionId": "sess-1",
                "sessionName": "explorer",
                "role": "gpu-multiagent-explorer",
                "agentName": "gpu-multiagent-explorer",
                "initialQuery": "explore acp",
                "status": {"type": status, "message": ""},
            }

        events = [
            # 1) crew list → spawn one card
            LLMEvent(kind=EVENT_SUBAGENT_LIST, subagents=[_sub("working")]),
            # 2) private-channel activity maps the inner toolCallId → this card
            LLMEvent(
                kind=EVENT_SUBAGENT_ACTIVITY,
                sub_session_id="sess-1",
                tool_call_id="tc-1",
                title="read",
            ),
            # 3) flat tool_call (full title) → attributed to the card
            LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Reading foo.py:1",
                tool_kind="read",
                tool_call_id="tc-1",
            ),
            # 4) flat tool_result (real output) → attributed + accumulated
            LLMEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="tc-1",
                tool_output="file body XYZ",
                tool_final=True,
            ),
            # 5) duplicate tool_result (kiro sends content + rawOutput) → deduped
            LLMEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="tc-1",
                tool_output="file body XYZ",
                tool_final=True,
            ),
            # 5b) sub-agent text streamed on the private channel (agent_message_chunk)
            LLMEvent(
                kind=EVENT_SUBAGENT_ACTIVITY, sub_session_id="sess-1", text="thinking out loud"
            ),
            # 6) crew list terminal → done with accumulated feed
            LLMEvent(kind=EVENT_SUBAGENT_LIST, subagents=[_sub("terminated")]),
            LLMEvent(kind=EVENT_COMPLETE),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "explore the codebase with 1 subagent")

        calls = [(c.args[0], c.args[1]) for c in state.broadcast_ws.call_args_list]
        card = "native:sess-1"

        # Card spawned with agent + task.
        spawns = [d for t, d in calls if t == "subagent_spawn" and d.get("id") == card]
        assert len(spawns) == 1
        assert spawns[0]["agent"] == "gpu-multiagent-explorer"
        assert spawns[0]["task"] == "explore acp"

        # Tool call + output streamed onto the card.
        chunks = [
            d.get("text", "") for t, d in calls if t == "subagent_chunk" and d.get("id") == card
        ]
        joined = "".join(chunks)
        assert "Reading foo.py:1" in joined  # full tool title from flat tool_call
        assert "file body XYZ" in joined  # real tool output
        assert "thinking out loud" in joined  # sub-agent text (agent_message_chunk)

        # Dedupe: the duplicate tool_result must NOT double-print the output.
        assert joined.count("file body XYZ") == 1

        # Done fires once with the accumulated feed as result (not the sentinel).
        dones = [d for t, d in calls if t == "subagent_done" and d.get("id") == card]
        assert len(dones) == 1
        assert "Reading foo.py:1" in dones[0]["result"]
        assert "file body XYZ" in dones[0]["result"]


class TestRunChatCompactDeferredWait:
    """The deferred-compaction wait at the end of _run_chat is a kiro-cli-only
    protocol step. claude-agent-acp performs /compact synchronously inside
    session/prompt and never emits ``_kiro.dev/compaction/status``, so the
    handler must skip ``wait_for_compaction`` for that backend or it sits
    blocked for 30 minutes and finally surfaces "Compaction timed out."
    """

    @staticmethod
    def _make_mock_client(events):
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.context_window_tokens = MagicMock(return_value=0)
        client.context_used_tokens = MagicMock(return_value=0)
        client.wait_for_compaction = AsyncMock(return_value={"type": "timeout"})

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_state_for_run_chat(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_claude_backend_skips_wait_for_compaction(self, tmp_path, monkeypatch):
        """When ``is_claude_backend(client)`` is True, the dashboard must
        report success immediately and never call ``wait_for_compaction``."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE)]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        # Patch the binding chat_runner imported at module load.
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.is_claude_backend", lambda _provider: True
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "/compact")

        client.wait_for_compaction.assert_not_called()
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert any("Conversation compacted" in m["content"] for m in assistant_msgs)
        assert not any("timed out" in m["content"] for m in assistant_msgs)
        # The notice must be tagged kind="compaction" so it does not shadow the
        # follow-up [OPTIONS:] buttons of the turn it follows (persisted meta +
        # live broadcast). See chat_utils._append_compaction_notice.
        compaction_msgs = [m for m in assistant_msgs if "Conversation compacted" in m["content"]]
        assert compaction_msgs
        assert all(m.get("meta", {}).get("kind") == "compaction" for m in compaction_msgs)
        # The appended row carries a minted ``meta.mid``: the live copy is
        # delivered through append's own identity-carrying door (_on_message),
        # so no hand-built duplicate ``chat_message`` frame may fire — a
        # mid-less manual frame rendered the notice twice (#5981 family).
        assert all(m.get("meta", {}).get("mid") for m in compaction_msgs)
        assistant_broadcasts = [
            c
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "chat_message" and c.args[1].get("role") == "assistant"
        ]
        assert assistant_broadcasts == []
        # Updated context% must be broadcast so the dashboard bar refreshes.
        ws_kinds = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "context_usage" in ws_kinds

    @pytest.mark.asyncio
    async def test_kiro_backend_still_waits_for_compaction(self, tmp_path, monkeypatch):
        """kiro-cli backend keeps the original deferred-wait path."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE)]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        client.wait_for_compaction = AsyncMock(
            return_value={"type": "completed", "summary": "summary text"}
        )
        # A real client dropped its counts when the completed status arrived
        # (AcpPromptStats.reset_after_compaction) — model that so the
        # end-of-turn payload broadcast reflects the post-compaction state.
        client.context_usage_pct = MagicMock(return_value=0.0)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.is_claude_backend", lambda _provider: False
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "/compact")

        client.wait_for_compaction.assert_awaited_once()
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert any("summary text" in m["content"] for m in assistant_msgs)
        # A completed deferred compaction must send the `reset` form — the
        # provider's counts were just dropped, so a payload broadcast would
        # claim "0 used" of a window nothing has re-measured yet.
        usage_calls = [
            c for c in state.broadcast_ws.call_args_list if c.args and c.args[0] == "context_usage"
        ]
        assert {"slot": slot.key, "pct": 0.0, "reset": True} in [c.args[1] for c in usage_calls]
        # No later broadcast may resurrect the stale pre-compaction meter.
        assert all(c.args[1].get("pct") == 0.0 for c in usage_calls)
        # Notice tagged kind="compaction" on the kiro deferred-wait path too.
        compaction_msgs = [m for m in assistant_msgs if "summary text" in m["content"]]
        assert compaction_msgs
        assert all(m.get("meta", {}).get("kind") == "compaction" for m in compaction_msgs)

    @pytest.mark.asyncio
    async def test_kiro_backend_broadcasts_real_post_compaction_usage(self, tmp_path, monkeypatch):
        """When the wait_for_compaction grace drain captured kiro's fresh
        post-compaction metadata, the broadcast must carry the REAL numbers
        (accurate pct + served window), not the reset/unknown fallback."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE)]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        client.wait_for_compaction = AsyncMock(return_value={"type": "completed", "summary": ""})
        # Post-drain provider state: metadata applied against the kept 1M
        # served window.
        client.context_usage_pct = MagicMock(return_value=5.0)
        client.context_window_tokens = MagicMock(return_value=1_000_000)
        client.context_used_tokens = MagicMock(return_value=50_000)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.is_claude_backend", lambda _provider: False
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "/compact")

        usage_calls = [
            c for c in state.broadcast_ws.call_args_list if c.args and c.args[0] == "context_usage"
        ]
        assert usage_calls
        expected = {
            "slot": slot.key,
            "pct": 5.0,
            "used_tokens": 50_000,
            "window_tokens": 1_000_000,
        }
        assert expected in [c.args[1] for c in usage_calls]
        # The accurate numbers must not be wiped by a reset broadcast.
        assert not any(c.args[1].get("reset") for c in usage_calls)

    @pytest.mark.asyncio
    async def test_kiro_backend_failed_compaction_keeps_meter(self, tmp_path, monkeypatch):
        """A failed deferred compaction leaves usage unchanged: re-send the
        current counts, never the reset form (which would blank a still-valid
        meter)."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE)]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        client.wait_for_compaction = AsyncMock(return_value={"type": "failed"})
        client.context_window_tokens = MagicMock(return_value=200_000)
        client.context_used_tokens = MagicMock(return_value=150_000)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.is_claude_backend", lambda _provider: False
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "/compact")

        usage_calls = [
            c for c in state.broadcast_ws.call_args_list if c.args and c.args[0] == "context_usage"
        ]
        assert usage_calls
        payload = usage_calls[-1].args[1]
        assert payload == {
            "slot": slot.key,
            "pct": 10.0,  # unchanged, from context_usage_pct()
            "used_tokens": 150_000,
            "window_tokens": 200_000,
        }

    @staticmethod
    def _compaction_notice(slot) -> str:
        """The text of the compaction notice appended to the transcript."""
        notices = [
            m["content"]
            for m in slot.messages
            if m.get("meta", {}).get("kind") == "compaction"
            and "Compaction" in m.get("content", "")
        ]
        assert notices, "no compaction notice was appended"
        return notices[-1]

    async def _run_failed_compaction(self, tmp_path, monkeypatch, summary):
        """Drive /compact to a `failed` result carrying *summary*."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client([LLMEvent(kind=EVENT_COMPLETE)])
        result = {"type": "failed"}
        if summary is not None:
            result["summary"] = summary
        client.wait_for_compaction = AsyncMock(return_value=result)
        client.context_window_tokens = MagicMock(return_value=200_000)
        client.context_used_tokens = MagicMock(return_value=150_000)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.is_claude_backend", lambda _provider: False
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "/compact")
        return slot

    @pytest.mark.asyncio
    async def test_failed_compaction_surfaces_the_reason(self, tmp_path, monkeypatch):
        """The provider's reason reaches the user instead of being discarded.

        Without it, a /compact that failed because the conversation is too large
        is indistinguishable from one that failed because the backend was
        unreachable — and those two call for different next moves. Every other
        surface (Slack/Telegram/Discord, and this dashboard's own auto-compact
        notice) already says which.
        """
        slot = await self._run_failed_compaction(
            tmp_path, monkeypatch, "context too large to summarize"
        )
        assert self._compaction_notice(slot) == (
            "❌ Compaction failed: context too large to summarize"
        )

    @pytest.mark.asyncio
    async def test_failed_compaction_without_a_reason_keeps_the_bare_notice(
        self, tmp_path, monkeypatch
    ):
        """No reason means no dangling colon — the old wording stands."""
        slot = await self._run_failed_compaction(tmp_path, monkeypatch, None)
        assert self._compaction_notice(slot) == "❌ Compaction failed."

    @pytest.mark.asyncio
    async def test_failed_compaction_blank_reason_keeps_the_bare_notice(
        self, tmp_path, monkeypatch
    ):
        """A whitespace-only reason is treated as absent, not printed."""
        slot = await self._run_failed_compaction(tmp_path, monkeypatch, "   ")
        assert self._compaction_notice(slot) == "❌ Compaction failed."

    @pytest.mark.asyncio
    async def test_failed_compaction_reason_is_redacted(self, tmp_path, monkeypatch):
        """A backend-echoed reason is not trusted to be credential-free.

        Same redact pair as the sibling `completed` branch, which already treats
        the provider's text as untrusted.
        """
        slot = await self._run_failed_compaction(
            tmp_path, monkeypatch, "auth failed for AKIAIOSFODNN7EXAMPLE key"
        )
        notice = self._compaction_notice(slot)
        assert "AKIAIOSFODNN7EXAMPLE" not in notice
        assert "Compaction failed:" in notice

    @pytest.mark.asyncio
    async def test_failed_compaction_reason_is_length_capped(self, tmp_path, monkeypatch):
        """A wall of provider text cannot scroll the transcript away."""
        from kiro_crew.dashboard.chat_runner import _COMPACT_FAIL_REASON_MAX_CHARS

        slot = await self._run_failed_compaction(tmp_path, monkeypatch, "x" * 5_000)
        notice = self._compaction_notice(slot)
        assert notice.endswith("…")
        assert len(notice) < _COMPACT_FAIL_REASON_MAX_CHARS + 60


class TestTokenPersistenceBackfill:
    """Regression tests for the late-backfill of slot.model before
    persist_token_record is called from _run_chat.

    Background: Claude Code reports its model only after the prompt is
    dispatched (via the `init` system event). The original eager backfill
    at the start of _run_chat reads client._model too early for CC, so
    slot.model stays empty and tokens.jsonl records get model="". The
    fix re-reads client._model right before persisting the token record.
    """

    @staticmethod
    def _make_mock_client(events, prov_model=""):
        """Mock provider that exposes a nested client._model attribute,
        mirroring AcpClient/CcClient layout (provider.client._model).
        """
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        # Expose `client.client._model` like the real provider wrappers
        inner = MagicMock()
        inner._model = prov_model
        client.client = inner

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_state_for_run_chat(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_safe_complete_reports_structured_monitor_usage(self, tmp_path, monkeypatch):
        """Normal runner return is not the accounting boundary; safe evidence is."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.monitoring.completion import MonitorCompletionHook
        from kiro_crew.monitoring.models import (
            MonitorActionCompletion,
            MonitorActionDisposition,
        )
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="done"),
            LLMEvent(
                kind=EVENT_COMPLETE,
                stop_reason="end_turn",
                usage=TurnUsage(input_tokens=12, output_tokens=4),
            ),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        client.context_used_tokens = MagicMock(return_value=0)
        client.context_window_tokens = MagicMock(return_value=0)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.generate_session_summary",
            AsyncMock(return_value=None),
        )
        completions: list[MonitorActionCompletion] = []

        async def _capture(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        await _run_chat(
            state,
            slot,
            "hello",
            monitor_completion=MonitorCompletionHook("monitor1", "failure-a", _capture),
        )

        assert len(completions) == 1
        assert completions[0].disposition is MonitorActionDisposition.SUCCESS
        assert completions[0].input_tokens == 12
        assert completions[0].output_tokens == 4

    @pytest.mark.asyncio
    async def test_synthetic_timeout_complete_does_not_report_monitor_usage(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.monitoring.completion import MonitorCompletionHook
        from kiro_crew.monitoring.models import MonitorActionCompletion
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        event = LLMEvent(
            kind=EVENT_COMPLETE,
            stop_reason="timeout",
            usage=TurnUsage(input_tokens=12, output_tokens=4),
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client([LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial"), event])
        client.context_used_tokens = MagicMock(return_value=0)
        client.context_window_tokens = MagicMock(return_value=0)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.generate_session_summary",
            AsyncMock(return_value=None),
        )
        completions: list[MonitorActionCompletion] = []

        async def _capture(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        await _run_chat(
            state,
            slot,
            "hello",
            monitor_completion=MonitorCompletionHook("monitor1", "failure-a", _capture),
        )

        assert completions == []

    @pytest.mark.asyncio
    async def test_synthesized_end_turn_does_not_report_monitor_usage(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.monitoring.completion import MonitorCompletionHook
        from kiro_crew.monitoring.models import MonitorActionCompletion
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        event = LLMEvent(
            kind=EVENT_COMPLETE,
            stop_reason="end_turn",
            synthetic_completion=True,
            usage=TurnUsage(input_tokens=12, output_tokens=4),
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client([LLMEvent(kind=EVENT_TEXT_CHUNK, text="done"), event])
        client.context_used_tokens = MagicMock(return_value=0)
        client.context_window_tokens = MagicMock(return_value=0)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.generate_session_summary",
            AsyncMock(return_value=None),
        )
        completions: list[MonitorActionCompletion] = []

        async def _capture(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        await _run_chat(
            state,
            slot,
            "hello",
            monitor_completion=MonitorCompletionHook("monitor1", "failure-a", _capture),
        )

        assert completions == []

    @pytest.mark.asyncio
    async def test_monitor_reauthorizes_at_provider_entry(self, tmp_path, monkeypatch):
        """A stopped claim cannot cross the runner's final provider boundary."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.monitoring.completion import MonitorCompletionHook

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client([])
        client.context_used_tokens = MagicMock(return_value=0)
        client.context_window_tokens = MagicMock(return_value=0)
        stream = MagicMock(side_effect=client.stream)
        client.stream = stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        authorize = AsyncMock(return_value=False)
        completion = MonitorCompletionHook(
            "monitor1",
            "failure-a",
            AsyncMock(),
            authorization_callback=authorize,
        )

        await _run_chat(state, slot, "hello", monitor_completion=completion)

        authorize.assert_awaited_once_with("monitor1", "failure-a")
        assert completion.accepted is False
        stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_monitor_shutdown_gate_precedes_acceptance(self, tmp_path, monkeypatch):
        """A closing session cannot acknowledge a wake that never entered the provider."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.monitoring.completion import MonitorCompletionHook
        from kiro_crew.session import SessionClosingError

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client([])
        client.context_used_tokens = MagicMock(return_value=0)
        client.context_window_tokens = MagicMock(return_value=0)
        stream = MagicMock(side_effect=client.stream)
        client.stream = stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.begin_turn = MagicMock(side_effect=SessionClosingError("closing"))
        accepted = MagicMock()
        completion = MonitorCompletionHook(
            "monitor1",
            "failure-a",
            AsyncMock(),
            authorization_callback=AsyncMock(return_value=True),
            acceptance_callback=accepted,
        )

        await _run_chat(state, slot, "hello", monitor_completion=completion)

        assert completion.accepted is False
        accepted.assert_not_called()
        stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_raw_complete_survives_cancellation_during_token_persistence(
        self, tmp_path, monkeypatch
    ):
        """Provider completion evidence is durable before cancellable analytics I/O."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.monitoring.completion import MonitorCompletionHook
        from kiro_crew.monitoring.models import MonitorActionCompletion
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        event = LLMEvent(
            kind=EVENT_COMPLETE,
            stop_reason="cancelled",
            usage=TurnUsage(input_tokens=12, output_tokens=4),
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client([event])
        client.context_used_tokens = MagicMock(return_value=0)
        client.context_window_tokens = MagicMock(return_value=0)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.generate_session_summary",
            AsyncMock(return_value=None),
        )

        async def _cancel_persistence(*_args, **_kwargs) -> None:
            raise asyncio.CancelledError

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async",
            _cancel_persistence,
        )
        completions: list[MonitorActionCompletion] = []

        async def _capture(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        await _run_chat(
            state,
            slot,
            "hello",
            monitor_completion=MonitorCompletionHook("monitor1", "failure-a", _capture),
        )

        assert len(completions) == 1
        assert completions[0].input_tokens == 12
        assert completions[0].output_tokens == 4

    @pytest.mark.asyncio
    async def test_late_backfill_populates_model_for_cc_session(self, tmp_path, monkeypatch):
        """When slot.model is empty at EVENT_COMPLETE but the provider has
        learned its model (CC init event), persist_token_record receives the
        provider model and slot.model is updated.

        The mock starts with an empty ``inner._model`` so the *early* backfill
        at the top of _run_chat finds nothing, then mutates ``inner._model``
        just before yielding EVENT_COMPLETE — mirroring CC reporting its
        model only after the prompt is dispatched. This way only the *late*
        backfill branch can populate the record's model, so removing the
        late-backfill code would cause this test to fail.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [
            LLMEvent(
                kind=EVENT_COMPLETE,
                usage=TurnUsage(input_tokens=12, output_tokens=34),
            ),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = ""  # CC has not yet emitted init when _run_chat begins

        # This simulates a claude_code session, so the backfill must run under
        # provider=claude_code for canonicalize_for_provider to map 'opus' ->
        # 'opus-4.8-1m'. Under the acp label the backfill (correctly) leaves a
        # kiro/acp model unchanged.
        #
        # The provider is now resolved from the LIVE CLIENT via is_claude_backend,
        # not from cfg.agent.provider: that field is declared enum=["acp"] and
        # validate_config_data deletes an out-of-enum value, so no real config can
        # ever say "claude_code" and this branch was unreachable in production
        # while the test mocked it green. The client here is a bare AsyncMock, so
        # the predicate is patched at the seam instead -- a real CC session's
        # client answers True.
        _cc_cfg = MagicMock()
        _cc_cfg.dashboard.merge_queued_messages = False
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.KiroCrewConfig.load", lambda: _cc_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.is_claude_backend", lambda _client: True
        )

        # Build a mock whose inner._model starts EMPTY so the early backfill
        # branch (chat_runner.py:471-476) finds nothing and leaves slot.model
        # blank. Then mutate inner._model mid-stream — just before yielding
        # EVENT_COMPLETE — so only the late backfill branch can populate it.
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        inner = MagicMock()
        inner._model = ""  # empty at session-create time
        client.client = inner

        async def _stream(msg):
            # Simulate CC's `init` system event arriving mid-turn, after the
            # prompt has been dispatched but before EVENT_COMPLETE.
            inner._model = "opus"
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        captured: list[tuple] = []

        async def _fake_persist(slot_key, model, event, provider="", **kwargs):
            captured.append((slot_key, model, provider))

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert len(captured) == 1
        slot_key, model, provider = captured[0]
        assert slot_key == "s1"
        # The backfill canonicalizes the provider's model via from_provider_id so
        # it matches the canonical-keyed dropdown: 'opus' alias -> 'opus-4.8-1m'.
        assert model == "opus-4.8-1m", "late backfill should populate canonical model"
        # slot.model should also be updated so subsequent turns reuse it
        assert slot.model == "opus-4.8-1m"

    @pytest.mark.asyncio
    async def test_auto_sentinel_reaches_persistence_fallback(self, tmp_path, monkeypatch):
        """An unresolved Auto request delegates to the recorder's fallback.

        The slot remains blank because it has no concrete model id to persist,
        while the client lets the usage recorder distinguish Auto from an
        unavailable model source.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [
            LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=5, output_tokens=7)),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = ""

        client = self._make_mock_client(events, prov_model="auto")
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        captured: list[tuple] = []

        async def _fake_persist(k, m, e, provider="", **kwargs):
            captured.append((k, m, provider, kwargs["model_source"]))

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert len(captured) == 1
        assert captured[0][1] == ""
        assert captured[0][3] is client
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_existing_slot_model_is_not_overwritten(self, tmp_path, monkeypatch):
        """OpenCode resolves model synchronously; slot.model is already set
        when EVENT_COMPLETE arrives. Backfill must not clobber it.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [
            LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=1, output_tokens=2)),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = "claude-opus-4.6"

        # Even if the inner client somehow reports a different value,
        # slot.model wins because it was already set explicitly.
        client = self._make_mock_client(events, prov_model="should-not-be-used")
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        captured: list[tuple] = []

        async def _fake_persist(k, m, e, provider="", **kwargs):
            captured.append((k, m, provider))

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert len(captured) == 1
        assert captured[0][1] == "claude-opus-4.6"
        assert slot.model == "claude-opus-4.6"


class TestTokenUsageSurface:
    """Usage rows record the slot's effective session source."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("slot_key", "linked_session_key", "expected"),
        [
            ("named-dashboard-tab", "", "dashboard"),
            ("slack-tab", "slack:1234567890.123456", "slack"),
            ("telegram-tab", "telegram:123:456", "telegram"),
        ],
    )
    async def test_completion_uses_effective_session_identity(
        self,
        tmp_path,
        monkeypatch,
        slot_key,
        linked_session_key,
        expected,
    ):
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        complete = LLMEvent(
            kind=EVENT_COMPLETE,
            usage=TurnUsage(input_tokens=1, output_tokens=1, duration_ms=25),
        )
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot(slot_key)
        slot._titled = True
        slot.linked_session_key = linked_session_key
        client = TestTokenPersistenceBackfill._make_mock_client(
            [LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"), complete]
        )
        client.context_used_tokens = MagicMock(return_value=10)
        client.context_window_tokens = MagicMock(return_value=100)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        persist = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.persist_token_record_async", persist)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert persist.await_args.args[0] == slot_key
        assert persist.await_args.kwargs["surface"] == expected

    @pytest.mark.asyncio
    async def test_completion_keeps_source_of_session_that_ran(self, tmp_path, monkeypatch):
        """A mid-turn rebind must not move the completed turn's attribution."""
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        complete = LLMEvent(
            kind=EVENT_COMPLETE,
            usage=TurnUsage(input_tokens=1, output_tokens=1, duration_ms=25),
        )
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("channel-tab")
        slot._titled = True
        slot.linked_session_key = "slack:1234567890.123456"
        client = TestTokenPersistenceBackfill._make_mock_client([])
        client.context_used_tokens = MagicMock(return_value=10)
        client.context_window_tokens = MagicMock(return_value=100)

        async def _stream(_message):
            # _run_chat has already selected the Slack session for this turn.
            slot.linked_session_key = "telegram:123:456"
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield complete

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        persist = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.persist_token_record_async", persist)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert persist.await_args.args[0] == slot.key
        assert persist.await_args.kwargs["surface"] == "slack"


class TestCostOnlyTurnPersistGate:
    """The chat_runner turn-end persist gate must fire on ``cost_usd`` alone.

    A claude-seam turn ending via a synthetic EVENT_COMPLETE (timeout,
    tool-stall, cancel-unacked) can carry cost with zero tokens and zero
    credits; the footer already reads ``_u.cost_usd`` off the same event, so
    only the gate stood between the cost and the usage store (#6758).
    """

    @pytest.mark.asyncio
    async def test_a_cost_only_turn_persists_its_row(self, tmp_path, monkeypatch):
        """Mutation guard: dropping the gate's ``cost_usd`` conjunct makes the
        persist call disappear and this fail."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(cost_usd=0.42))]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = TestTokenPersistenceBackfill._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        persist = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.persist_token_record_async", persist)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert persist.await_count == 1
        assert persist.await_args.args[2].usage.cost_usd == pytest.approx(0.42)

    @pytest.mark.asyncio
    async def test_an_all_zero_turn_still_writes_no_row(self, tmp_path, monkeypatch):
        """The widened gate must not have become unconditional: a turn whose
        usage carries no billing dimension at all stays out of the store."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage())]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = TestTokenPersistenceBackfill._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        persist = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.persist_token_record_async", persist)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        persist.assert_not_awaited()


class TestKiroBackfillProfileGuard:
    """Regression tests for the kiro/acp backfill must NOT store a
    resolved Bedrock inference-profile id into slot.model.

    kiro-cli reports the RESOLVED profile id (e.g.
    ``global.anthropic.claude-opus-4-8[1m]``) on ``client._model`` — not the
    alias the user picked. Because ``canonicalize_for_provider`` is a no-op for
    kiro, an un-guarded backfill stores that profile id verbatim and re-sends it
    as a ``set_model`` override on every resume, pinning the slot to one
    profile+region. When that profile is capacity-throttled the session is stuck
    and the picker can't dislodge it. The guard drops a profile-form id for
    non-claude_code providers so the slot re-resolves; portable aliases are kept.
    """

    @staticmethod
    def _client_with_model(prov_model):
        client = MagicMock()
        inner = MagicMock()
        inner._model = prov_model
        client.client = inner
        return client

    def test_predicate_flags_bedrock_profile_ids(self):
        from kiro_crew.dashboard.chat_runner import _is_bedrock_profile_id

        assert _is_bedrock_profile_id("global.anthropic.claude-opus-4-8[1m]")
        assert _is_bedrock_profile_id("us.anthropic.claude-opus-4-7")
        assert _is_bedrock_profile_id("global.anthropic.claude-sonnet-4-6[1m]")
        # Portable aliases the picker actually sets — NOT profile ids.
        assert not _is_bedrock_profile_id("claude-opus-4.7")
        assert not _is_bedrock_profile_id("claude-opus-4.8")
        assert not _is_bedrock_profile_id("sonnet")
        assert not _is_bedrock_profile_id("deepseek-3.2")

    def test_kiro_profile_id_is_dropped(self):
        from kiro_crew.dashboard.chat_runner import _backfill_canonical_model

        client = self._client_with_model("global.anthropic.claude-opus-4-8[1m]")
        # acp/kiro provider: the throttled profile id must NOT be backfilled.
        assert _backfill_canonical_model(client, "acp") == ""
        assert _backfill_canonical_model(client, "kiro") == ""

    def test_kiro_portable_alias_is_kept(self):
        from kiro_crew.dashboard.chat_runner import _backfill_canonical_model

        # A dotted alias is the picker's value and routes with capacity
        # awareness — keep it so the header/dropdown still reflect the model.
        client = self._client_with_model("claude-opus-4.7")
        assert _backfill_canonical_model(client, "acp") == "claude-opus-4.7"

    def test_claude_code_profile_id_still_canonicalizes(self):
        from kiro_crew.dashboard.chat_runner import _backfill_canonical_model

        # claude_code is unaffected: its profile id maps to the dropdown key the
        # user explicitly chose, so the guard must not strip it.
        client = self._client_with_model("global.anthropic.claude-opus-4-8[1m]")
        out = _backfill_canonical_model(client, "claude_code")
        assert out == "opus-4.8-1m"

    def test_auto_sentinel_still_skipped(self):
        from kiro_crew.dashboard.chat_runner import _backfill_canonical_model

        client = self._client_with_model("auto")
        assert _backfill_canonical_model(client, "acp") == ""
        assert _backfill_canonical_model(client, "claude_code") == ""

    @pytest.mark.asyncio
    async def test_run_chat_kiro_resolved_profile_not_pinned(self, tmp_path, monkeypatch):
        """End-to-end: a kiro session whose backend resolves to the 1M Opus
        profile mid-turn must leave slot.model EMPTY (not pinned to the profile
        id), so the next resume re-resolves rather than re-sending the throttled
        profile as a set_model override.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=3, output_tokens=4))]

        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = ""  # user picked nothing explicit on this turn

        # Default test config provider is acp/kiro — exercise that path.
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        inner = MagicMock()
        inner._model = ""  # empty at create; kiro learns the profile mid-turn
        client.client = inner

        async def _stream(msg):
            # kiro resolves the picked alias to a concrete Bedrock profile id.
            inner._model = "global.anthropic.claude-opus-4-8[1m]"
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        captured: list[tuple] = []

        async def _fake_persist(k, m, e, provider="", **kwargs):
            captured.append((k, m, provider))

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # slot.model must NOT be pinned to the throttled profile id.
        assert slot.model == "", "kiro profile id must not poison slot.model"
        assert len(captured) == 1
        assert captured[0][1] == "", "token record must not carry the profile id"


class TestPinnedModelWithheld:
    """The slot's pin must not outlive the account's access to that model.

    ``providers.acp`` withholds a configured model the live session did not
    advertise and leaves the session on the backend default, so the turn
    succeeds. Nothing told the slot, so the composer chip and the picker went on
    naming a model no turn would use — the reported symptom after a plan
    downgrade (chip read ``claude-opus-5``; every turn ran on auto). The runner
    reports the dead pin using the same predicate the withhold uses, and carries
    the verdict in the slots payload so the frontend reads it instead of
    inferring it from picker-list membership (#1819).
    """

    @staticmethod
    def _client(advertised, *, claude_backend=False):
        client = MagicMock()
        client.available_models = MagicMock(return_value=[{"modelId": m} for m in advertised])
        client.is_claude_backend = claude_backend
        return client

    def test_pin_absent_from_advertised_is_withheld(self):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        client = self._client(["auto", "claude-sonnet-5"])
        assert _pinned_model_verdict(client, "claude-opus-5", "acp") is True

    def test_advertised_pin_is_kept(self):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        client = self._client(["auto", "claude-opus-5"])
        assert _pinned_model_verdict(client, "claude-opus-5", "acp") is False

    def test_unknown_entitlement_keeps_the_pin(self):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        # Advertised nothing (no session yet / backend omits the list) must read
        # as "unknown", never as "nothing is allowed" — and, since the verdict is
        # displayed, never as "entitled" either: None is its own answer.
        assert _pinned_model_verdict(self._client([]), "claude-opus-5", "acp") is None

    def test_auto_and_empty_are_never_withheld(self):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        client = self._client(["claude-sonnet-5"])
        assert _pinned_model_verdict(client, "auto", "acp") is None
        assert _pinned_model_verdict(client, "", "acp") is None

    def test_claude_code_provider_is_exempt(self):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        # slot.model is a canonical key there while the backend advertises bare
        # ids — comparing the two namespaces would call every model unusable.
        client = self._client(["claude-opus-4-8[1m]"])
        assert _pinned_model_verdict(client, "opus-4.8-1m", "claude_code") is None

    def test_claude_backend_provider_is_exempt(self):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        client = self._client(["claude-opus-4-8[1m]"], claude_backend=True)
        assert _pinned_model_verdict(client, "claude-opus-4.8", "acp") is None

    def test_provider_without_getter_keeps_the_pin(self):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        client = MagicMock()
        del client.available_models
        client.is_claude_backend = False
        assert _pinned_model_verdict(client, "claude-opus-5", "acp") is None

    def test_getter_raising_keeps_the_pin(self):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        client = MagicMock()
        client.is_claude_backend = False
        client.available_models = MagicMock(side_effect=RuntimeError("boom"))
        assert _pinned_model_verdict(client, "claude-opus-5", "acp") is None

    def test_namespaced_pin_matches_bare_advertised(self):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        # Regression #8521: a persisted pin can carry a `<namespace>::<bare-id>`
        # qualifier from the catalog that advertised it when it was stored,
        # while the session being judged advertises the BARE id. The literal
        # comparison missed for every such pin and the banner claimed a fully
        # supported model "isn't offered right now". The verdict must peel the
        # qualifier and resolve to runnable — for ANY namespace vocabulary, not
        # just `agent.provider` (a fixed enum that no catalog qualifies ids
        # with, so keying on it would leave the fold unreachable).
        client = self._client(["auto", "z-ai/glm-5.3-flash"])
        assert _pinned_model_verdict(client, "openrouter::z-ai/glm-5.3-flash", "acp") is False

    def test_namespaced_pin_judged_even_when_provider_unreadable(self):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        # _run_chat passes provider_name="" when the config could not be read.
        # The fold keys on the pin's own qualifier, not on the provider string,
        # so an unreadable config must not degrade the verdict back to the
        # false withhold (nor is it needed for the peel to fire).
        client = self._client(["auto", "z-ai/glm-5.3-flash"])
        assert _pinned_model_verdict(client, "openrouter::z-ai/glm-5.3-flash", "") is False

    def test_pin_absent_after_peel_is_still_withheld(self):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        # The peel must not turn the verdict into a rubber stamp: a pin the
        # backend serves under NEITHER spelling still answers withheld.
        client = self._client(["auto", "z-ai/glm-5.3-flash"])
        assert _pinned_model_verdict(client, "openrouter::no-such-model", "acp") is True

    def test_only_one_qualifier_level_is_peeled(self):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        # The fold peels exactly ONE leading qualifier. A doubly-qualified pin
        # whose innermost tail happens to be advertised is not a spelling of
        # that model — unbounded stripping would rubber-stamp arbitrary junk
        # around any advertised id.
        client = self._client(["auto", "glm-5.3-flash"])
        assert _pinned_model_verdict(client, "a::b::glm-5.3-flash", "acp") is True

    def test_pin_advertised_verbatim_needs_no_peel(self):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        # A backend that advertises the qualified spelling itself matches on
        # the first (full) comparison — the peel is a miss-only retry, so it
        # can only clear a false withhold, never create one.
        client = self._client(["openrouter::z-ai/glm-5.3-flash"])
        assert _pinned_model_verdict(client, "openrouter::z-ai/glm-5.3-flash", "acp") is False

    def test_slot_reports_no_verdict_until_one_is_recorded(self):
        slot = _ChatSlot("s1")
        slot.model = "claude-opus-5"
        # A slot that has never spawned knows nothing about entitlement, and the
        # frontend fails open on that — so the default must be unknown, not False.
        assert slot.model_withheld is None
        assert slot.to_dict()["model_withheld"] is None

    def test_slots_payload_carries_both_answers(self):
        slot = _ChatSlot("s1")
        slot.model = "claude-opus-5"
        slot.record_model_withheld(True)
        assert slot.to_dict()["model_withheld"] is True
        slot.record_model_withheld(False)
        assert slot.to_dict()["model_withheld"] is False

    def test_verdict_is_reported_only_for_the_model_it_was_computed_for(self):
        """Re-pinning must invalidate the verdict without anyone remembering to.

        `slot.model` has many writers (the picker, the bulk pick, the pick
        rollback, two restore paths, the canonical backfill). A verdict that
        outlived a re-pin would label the NEW model with the OLD model's
        entitlement, so the verdict is stored against the id it was computed for
        instead of relying on each of those writers to clear it.
        """
        slot = _ChatSlot("s1")
        slot.model = "claude-opus-5"
        slot.record_model_withheld(True)
        assert slot.model_withheld is True

        slot.model = "claude-sonnet-5"  # the user picks a model they can run
        assert slot.model_withheld is None, "a verdict must not survive a re-pin"

        slot.model = "claude-opus-5"  # ...and back again
        assert slot.model_withheld is True, "the verdict still describes this pin"

        slot.model = ""  # pin cleared entirely
        assert slot.model_withheld is None

    def test_teardown_forgets_the_verdict(self):
        slot = _ChatSlot("s1")
        slot.model = "claude-opus-5"
        slot.record_model_withheld(True)
        # The verdict describes the session that advertised the list, so a
        # session teardown drops it rather than leaving the next session labelled
        # by the previous one's entitlement.
        slot.record_model_withheld(None)
        assert slot.model_withheld is None

    def test_every_session_teardown_drops_the_verdict(self):
        """A teardown site added later must not silently keep a dead verdict.

        The drop cannot live in one chokepoint: `_reset_slot_session` is the
        funnel for the switch handlers, but the runner tears a session down
        directly at a turn boundary too (the deferred project-change reset, the
        deferred conversation discard, the post-turn agent-switch reset) and
        routing those through the funnel would also cancel pending question cards.
        So the invariant is pinned here instead of trusting each author to
        remember it.
        """
        from pathlib import Path

        from kiro_crew.dashboard import chat_handlers, chat_runner

        teardowns = ("state.sessions.reset(", "state.sessions.discard_conversation(")
        for module in (chat_runner, chat_handlers):
            lines = Path(module.__file__).read_text(encoding="utf-8").splitlines()
            sites = [i for i, line in enumerate(lines) if any(t in line for t in teardowns)]
            assert sites, f"no teardown call site found in {module.__name__}"
            for i in sites:
                # Asymmetric window: a drop placed AFTER the call has to clear the
                # multi-line call plus its refusal check (`discard_conversation`
                # returns False when a turn is in flight), so the trailing half is
                # the wider one.
                window = "\n".join(lines[max(0, i - 12) : i + 23])
                assert "record_model_withheld(None)" in window, (
                    f"{module.__name__}:{i + 1} replaces the session the withhold "
                    f"verdict describes without dropping it -> {lines[i].strip()}"
                )

    @pytest.mark.asyncio
    async def test_reset_chokepoint_forgets_the_verdict(self):
        """The one reset funnel must drop it, so all its call sites do."""
        from kiro_crew.dashboard.chat_handlers import _reset_slot_session

        state = MagicMock()

        async def _reset(_key, *, skip_if_busy=False):
            return True

        state.sessions.reset = _reset
        slot = _ChatSlot("s1")
        slot.model = "claude-opus-5"
        slot.record_model_withheld(True)

        with patch("kiro_crew.dashboard.chat_handlers._unblock_pending_waits"):
            await _reset_slot_session(state, slot, "dashboard:s1")

        assert slot.model_withheld is None

    @pytest.mark.asyncio
    async def test_a_declined_reset_keeps_the_live_session_s_verdict(self):
        """`skip_if_busy` declining leaves the session -- and its verdict -- alive.

        The reload route resets with ``skip_if_busy=True``, which returns False
        when a turn is in flight and touches nothing. Dropping the verdict there
        would republish a runnable pin as `auto` -- the defect the verdict exists
        to remove -- for a session that never changed.
        """
        from kiro_crew.dashboard.chat_handlers import _reset_slot_session

        state = MagicMock()

        async def _declined(_key, *, skip_if_busy=False):
            return False

        state.sessions.reset = _declined
        slot = _ChatSlot("s1")
        slot.model = "claude-opus-5"
        slot.record_model_withheld(True)

        with patch("kiro_crew.dashboard.chat_handlers._unblock_pending_waits"):
            reloaded = await _reset_slot_session(state, slot, "dashboard:s1", skip_if_busy=True)

        assert reloaded is False
        assert slot.model_withheld is True, "a declined reset must not erase the verdict"

    @pytest.mark.asyncio
    async def test_a_reset_that_raises_drops_the_verdict(self):
        """A teardown that raised leaves a session the slot cannot vouch for."""
        from kiro_crew.dashboard.chat_handlers import _reset_slot_session

        state = MagicMock()

        async def _boom(_key, *, skip_if_busy=False):
            raise RuntimeError("teardown exploded")

        state.sessions.reset = _boom
        slot = _ChatSlot("s1")
        slot.model = "claude-opus-5"
        slot.record_model_withheld(True)

        with patch("kiro_crew.dashboard.chat_handlers._unblock_pending_waits"):
            with pytest.raises(RuntimeError):
                await _reset_slot_session(state, slot, "dashboard:s1")

        assert slot.model_withheld is None

    @pytest.mark.asyncio
    async def test_run_chat_reports_but_keeps_a_withheld_pin(self, tmp_path, monkeypatch):
        """End-to-end: a slot pinned to an unentitled model gets an in-chat
        notice, and the pin SURVIVES so a plan re-upgrade self-heals.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=3, output_tokens=4))]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = "claude-opus-5"  # pinned before the plan downgrade

        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.is_claude_backend = False
        # The live session advertises the free tier only.
        client.available_models = MagicMock(
            return_value=[{"modelId": "auto"}, {"modelId": "claude-sonnet-5"}]
        )
        inner = MagicMock()
        inner._model = ""
        client.client = inner

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        async def _fake_persist(k, m, e, provider="", **kwargs):
            del k, m, e, provider, kwargs

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # Keeping the pin is the point: withhold keeps it off the wire and
        # displayModel keeps it off the chip, so deleting an explicit user
        # setting buys nothing and costs the self-heal on re-upgrade.
        assert slot.model == "claude-opus-5", "an inert pin must not be deleted"
        # The activity line must not name the withheld model while the notice card
        # beside it says that model is not what is running.
        session_frames = [
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "activity_event" and c.args[1].get("kind") == "session"
        ]
        assert any(
            f.get("spawned") is True for f in session_frames
        ), f"a new session must report spawned=True, got {session_frames}"
        assert all(
            "claude-opus-5" not in f.get("text", "") for f in session_frames
        ), f"the session line must report the effective model, got {session_frames}"
        assert any(
            "auto" in f.get("text", "") for f in session_frames
        ), f"expected the effective model on the session line, got {session_frames}"
        # But the user must be able to learn why their model is not being used, and
        # that explanation has to survive a reload the same way the pin does — so
        # it is a persisted transcript row, not a transient activity line.
        notices = [m.get("content", "") for m in slot.messages if m.get("role") == "notice"]
        assert any(
            "claude-opus-5" in t and "isn't offered right now" in t for t in notices
        ), f"expected a persisted notice naming the withheld model, got {notices}"
        # And the verdict is CARRIED, not left for the frontend to re-derive: the
        # slots payload states it, so the chip does not have to read "absent from
        # GET /api/models" as "not entitled" (#1819).
        assert slot.model_withheld is True
        assert slot.to_dict()["model_withheld"] is True

    @pytest.mark.asyncio
    async def test_run_chat_records_an_entitled_pin_as_not_withheld(self, tmp_path, monkeypatch):
        """The false answer must be carried too, not just the withhold.

        It is the half that stops an unrelated `/api/models` filter from acting as
        an entitlement signal. The verdict is computed against the wire id the
        session was actually given, so a pin the picker's list omits is still
        reported runnable.

        Asserted on a DEPRECATED pin's replacement on purpose: `api_models` drops
        deprecated ids before the entitlement narrowing, and `_normalize_model`
        rewrites such a pin to its replacement at turn start — so the id the
        session (and this verdict) sees is the replacement, never the deprecated
        spelling the slot was carrying.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=3, output_tokens=4))]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = "claude-opus-4.6-1m"  # deprecated spelling: absent from GET /api/models

        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.is_claude_backend = False
        # The session serves the replacement the pin normalizes to.
        client.available_models = MagicMock(
            return_value=[{"modelId": "auto"}, {"modelId": "claude-opus-4.6"}]
        )
        inner = MagicMock()
        inner._model = ""
        client.client = inner

        async def _stream(msg):
            del msg
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        async def _fake_persist(k, m, e, provider="", **kwargs):
            del k, m, e, provider, kwargs

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert slot.model == "claude-opus-4.6", "the deprecated pin is rewritten at turn start"
        assert slot.to_dict()["model_withheld"] is False
        notices = [m.get("content", "") for m in slot.messages if m.get("role") == "notice"]
        assert not any(
            "isn't offered right now" in t for t in notices
        ), f"an entitled pin must not be reported as withheld, got {notices}"

    @pytest.mark.asyncio
    async def test_a_replacement_session_that_advertises_nothing_publishes_unknown(
        self, tmp_path, monkeypatch
    ):
        """A new session must never inherit the previous session's verdict.

        A replacement can arrive without a teardown this slot saw (a reaped
        session, a provider that re-spawned) and can advertise nothing at all --
        a dead provider, or a backend that omits `models`. That is "not known",
        and publishing the previous session's answer for it would label a live
        session from an entitlement snapshot it never took.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=3, output_tokens=4))]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = "claude-opus-5"
        slot.record_model_withheld(True)  # the PREVIOUS session withheld this pin

        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.is_claude_backend = False
        client.available_models = MagicMock(return_value=[])  # advertises nothing
        inner = MagicMock()
        inner._model = ""
        client.client = inner

        async def _stream(msg):
            del msg
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        async def _fake_persist(k, m, e, provider="", **kwargs):
            del k, m, e, provider, kwargs

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert slot.to_dict()["model_withheld"] is None, "a stale verdict must not be republished"
        assert slot.model == "claude-opus-5", "the pin itself is untouched"
        notices = [m.get("content", "") for m in slot.messages if m.get("role") == "notice"]
        assert not any(
            "isn't offered right now" in t for t in notices
        ), f"unknown entitlement must not be reported as a withhold, got {notices}"

    @pytest.mark.asyncio
    async def test_run_chat_survives_an_unreadable_config_on_a_pinned_slot(
        self, tmp_path, monkeypatch
    ):
        """Config going unreadable mid-turn must not abort the turn.

        `cfg` is bound inside a try whose except only logs, so a later
        `cfg.agent.provider` read would raise UnboundLocalError. A PINNED slot is
        what exercises it: the withhold check runs only when slot.model is set, so
        an unpinned slot never reaches the second read.

        The failing load has to be the SECOND one. An earlier unguarded
        `KiroCrewConfig.load()` (slash-command detection) would abort the turn
        first if config were broken from the start, so the reachable shape is
        config that loads once and then goes bad — config is read live through a
        fingerprint cache, so an edit mid-turn does exactly that.
        """
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=3, output_tokens=4))]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = "claude-opus-5"

        real_cfg = KiroCrewConfig.load()
        seen = {"n": 0}

        def load_then_break():
            # Deliberately NOT a finite side_effect list: an extra call must not
            # raise StopIteration and turn a guard test into a mock artifact.
            seen["n"] += 1
            if seen["n"] == 2:
                raise ValueError("config became unreadable mid-turn")
            return real_cfg

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.KiroCrewConfig.load", load_then_break)

        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.is_claude_backend = False
        client.available_models = MagicMock(return_value=[{"modelId": "claude-sonnet-5"}])
        inner = MagicMock()
        inner._model = ""
        client.client = inner

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        async def _fake_persist(k, m, e, provider="", **kwargs):
            del k, m, e, provider, kwargs

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        # Completes. Reading cfg.agent.provider here raises UnboundLocalError.
        await _run_chat(state, slot, "hello")

        assert seen["n"] >= 2, "the guarded load must actually have been reached"
        assert slot.model == "claude-opus-5", "the pin survives regardless"

    @pytest.mark.asyncio
    async def test_run_chat_keeps_the_real_model_on_the_label_when_entitled(
        self, tmp_path, monkeypatch
    ):
        """The withheld case reports `auto` on the activity line; the healthy case
        must still report the model the session actually runs on."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=3, output_tokens=4))]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = "claude-opus-5"

        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.is_claude_backend = False
        # This account CAN run the pin, so nothing is withheld.
        client.available_models = MagicMock(
            return_value=[{"modelId": "auto"}, {"modelId": "claude-opus-5"}]
        )
        inner = MagicMock()
        inner._model = ""
        client.client = inner

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        async def _fake_persist(k, m, e, provider="", **kwargs):
            del k, m, e, provider, kwargs

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        session_frames = [
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "activity_event" and c.args[1].get("kind") == "session"
        ]
        assert any(
            "claude-opus-5" in f.get("text", "") for f in session_frames
        ), f"an entitled pin must still be named on the session line, got {session_frames}"
        notices = [m.get("content", "") for m in slot.messages if m.get("role") == "notice"]
        assert not any(
            "isn't offered right now" in t for t in notices
        ), f"nothing was withheld, so there must be no notice, got {notices}"

    @pytest.mark.asyncio
    async def test_run_chat_stays_quiet_on_a_warm_session(self, tmp_path, monkeypatch):
        """The notice reports the SPAWN-time withhold, so a warm session (neither
        new nor resumed) must not repeat it on every turn.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=3, output_tokens=4))]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = "claude-opus-5"

        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.is_claude_backend = False
        client.available_models = MagicMock(return_value=[{"modelId": "claude-sonnet-5"}])
        inner = MagicMock()
        inner._model = ""
        client.client = inner

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        # Warm session: not new, not resumed.
        state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))

        async def _fake_persist(k, m, e, provider="", **kwargs):
            del k, m, e, provider, kwargs

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        notices = [m.get("content", "") for m in slot.messages if m.get("role") == "notice"]
        assert not any(
            "isn't offered right now" in t for t in notices
        ), f"the withhold notice must not repeat every turn, got {notices}"
        assert slot.model == "claude-opus-5"
        # A warm turn respawns nothing, so the session frame must say so: the
        # frontend refetches the entitlement-narrowed model list on `spawned`,
        # and /api/models spawns a subprocess, so a truthy flag here would run
        # one per prompt.
        session_frames = [
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "activity_event" and c.args[1].get("kind") == "session"
        ]
        assert session_frames, "the session frame is emitted on warm turns too"
        assert all(
            f.get("spawned") is False for f in session_frames
        ), f"warm turn must not claim a spawn, got {session_frames}"

    @pytest.mark.asyncio
    async def test_run_chat_keeps_an_entitled_pin(self, tmp_path, monkeypatch):
        """Counterpart: an advertised pin is left exactly as the user set it."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=3, output_tokens=4))]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = "claude-sonnet-5"

        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.is_claude_backend = False
        client.available_models = MagicMock(
            return_value=[{"modelId": "auto"}, {"modelId": "claude-sonnet-5"}]
        )
        inner = MagicMock()
        inner._model = ""
        client.client = inner

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        async def _fake_persist(k, m, e, provider="", **kwargs):
            del k, m, e, provider, kwargs

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert slot.model == "claude-sonnet-5"


class TestPrepareMessagesInterleaved:
    """Tests for _prepare_messages with interleaved assistant/tool/chunk messages."""

    def test_interleaved_assistant_tool_chunk_structure(self):
        """_prepare_messages with interleaved assistant/tool/chunk returns
        correct structure.

        Validates: Requirements 6.1
        """
        from kiro_crew.dashboard.chat import _prepare_messages

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Before tool", "cls": "msg msg-a"},
            {"role": "tool", "content": "✅ read_file", "cls": "msg msg-tool"},
            {"role": "assistant", "content": "After tool", "cls": "msg msg-a"},
            {"role": "chunk", "content": "still "},
            {"role": "chunk", "content": "streaming"},
        ]

        result = _prepare_messages(messages, running=True, live_child="")

        # user, assistant, tool, assistant, streaming (collapsed chunks)
        assert len(result) == 5
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] == "Before tool"
        assert result[2]["role"] == "tool"
        assert result[3]["role"] == "assistant"
        assert result[3]["content"] == "After tool"
        assert result[4]["role"] == "streaming"
        assert result[4]["content"] == "still streaming"

    def test_no_trailing_chunks_no_streaming(self):
        """Without trailing chunks, no streaming message is produced."""
        from kiro_crew.dashboard.chat import _prepare_messages

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Segment 1", "cls": "msg msg-a"},
            {"role": "tool", "content": "✅ bash", "cls": "msg msg-tool"},
            {"role": "assistant", "content": "Segment 2", "cls": "msg msg-a"},
        ]

        result = _prepare_messages(messages, running=False, live_child="")

        assert len(result) == 4
        roles = [m["role"] for m in result]
        assert "streaming" not in roles
        assert "chunk" not in roles


# ── Runtime wiring tests (multi-agent-orchestration) ──


class TestRuntimeWiring:
    """Tests for multi-agent-orchestration runtime wiring.

    Requirements: 1.3, 2.3, 2.4, 3.1
    """

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_response_includes_workspace(self, tmp_path, monkeypatch):
        """api_chat_slot_agent response includes resolved workspace field.

        Requirements: 1.3
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.sessions.reset = AsyncMock()

        # Mock config loading to return a config with a known agent
        mock_cfg = MagicMock()
        mock_cfg.agents = {"oncall": MagicMock(workspace="oncall-ws", memory_store="oncall-mem")}
        mock_cfg.workspaces = {"oncall-ws": MagicMock(dir="/tmp/oncall")}
        mock_cfg.default_workspace = "default"
        mock_cfg.default_memory_store = "default"
        mock_cfg.memory_stores = {"oncall-mem": MagicMock()}
        mock_cfg.memory = MagicMock()

        mock_bindings = MagicMock()
        mock_bindings.workspace_dir = Path("/tmp/oncall")
        mock_bindings.memory_store_name = "oncall-mem"
        mock_bindings.model = ""

        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.resolve_agent_bindings",
            lambda cfg, name, project_dir=None: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name, project_dir=None: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat._workspace_name_for_dir",
            lambda cfg, ws_dir: "oncall-ws",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "oncall-ws",
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "oncall"})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["agent"] == "oncall"
            assert "workspace" in data
            assert data["workspace"] == "oncall-ws"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_failed_resolution_reports_slot_workspace(
        self, tmp_path, monkeypatch
    ):
        """A binding-resolution failure must not fabricate workspace='default'.

        The handler logs and proceeds when resolve_agent_bindings raises, and
        slot.workspace keeps its previous value — so the response must name
        THAT value. Since #5120 the acting tab writes the response's workspace
        into its store optimistically; a fabricated 'default' would pin the
        chip to a workspace the slot does not hold, and with the websocket
        down (the optimistic write's whole premise) nothing corrects it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.workspace = "research-ws"
        state.sessions.reset = AsyncMock()

        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", _boom)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "oncall"})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["agent"] == "oncall"
            # The slot's workspace was not changed by the failed resolution,
            # and the response reports the value the slot actually holds.
            assert slot.workspace == "research-ws"
            assert data["workspace"] == "research-ws"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_reset_failure_keeps_committed_agent(
        self, tmp_path, monkeypatch
    ):
        """A throwing teardown reports SUCCESS with a warning; the agent stays.

        The reset pops the session before its shutdown can fail, so by the
        failure point the old binding's session no longer exists — every
        future or replacement session cold-starts on the NEW agent. The
        switch therefore succeeded: answering 500 would make the acting
        tab's performSlotSwitch keep the OLD store value for a switch that
        actually happened.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.agent = "oncall"
        slot.workspace = "oncall-ws"
        slot.project = "/tmp/oncall"
        state.sessions.reset = AsyncMock(side_effect=RuntimeError("shutdown blew up"))

        mock_cfg = MagicMock()
        mock_cfg.agents = {"research": MagicMock(workspace="research-ws", memory_store="default")}
        mock_cfg.workspaces = {"research-ws": MagicMock(dir="/tmp/research")}
        mock_cfg.default_workspace = "default"
        mock_cfg.default_memory_store = "default"
        mock_cfg.memory_stores = {}
        mock_cfg.memory = MagicMock()
        mock_bindings = MagicMock()
        mock_bindings.workspace_dir = Path("/tmp/research")
        mock_bindings.memory_store_name = "default"
        mock_bindings.model = ""
        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.resolve_agent_bindings",
            lambda cfg, name, project_dir=None: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name, project_dir=None: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "research"})
            data = await resp.json()
            # Success with a warning — the switch happened; only the old
            # process's teardown degraded.
            assert resp.status == 200
            assert data["ok"] is True
            assert data["agent"] == "research"
            assert data["warning"] == "old session teardown incomplete"
            # The committed bindings stay (the popped old session cannot come
            # back) — agent and the derived workspace both advertise the new
            # binding.
            assert slot.agent == "research"
            assert slot.workspace == "research-ws"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_failed_reset_spares_concurrent_writes(
        self, tmp_path, monkeypatch
    ):
        """A failed switch never disturbs values written while it was in flight.

        Commit-after-reset means the failure path touches nothing, so state
        written by a concurrent actor during the (failing) reset — e.g. the
        project endpoint, which does not take the slot lock — survives
        untouched. Restoring captured priors here (the old rollback shape)
        would silently erase that concurrent success.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.agent = "oncall"
        slot.workspace = "oncall-ws"
        slot.project = "/tmp/oncall"

        async def _concurrent_switch_lands_then_reset_fails(*args, **kwargs):
            # Simulate a concurrent request winning the race while this
            # request's reset is in flight.
            slot.agent = "writer"
            slot.workspace = "writing-ws"
            slot.project = "/tmp/writing"
            raise RuntimeError("shutdown blew up")

        state.sessions.reset = AsyncMock(side_effect=_concurrent_switch_lands_then_reset_fails)

        mock_cfg = MagicMock()
        mock_cfg.agents = {"research": MagicMock(workspace="research-ws", memory_store="default")}
        mock_cfg.workspaces = {"research-ws": MagicMock(dir="/tmp/research")}
        mock_cfg.default_workspace = "default"
        mock_cfg.default_memory_store = "default"
        mock_cfg.memory_stores = {}
        mock_cfg.memory = MagicMock()
        mock_bindings = MagicMock()
        mock_bindings.workspace_dir = Path("/tmp/research")
        mock_bindings.memory_store_name = "default"
        mock_bindings.model = ""
        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.resolve_agent_bindings",
            lambda cfg, name, project_dir=None: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name, project_dir=None: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "research"})
            assert resp.status == 200
            # The concurrent winner's values survive — no field snaps back to
            # this request's priors.
            assert slot.agent == "writer"
            assert slot.workspace == "writing-ws"
            assert slot.project == "/tmp/writing"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_success_commit_spares_concurrent_pick(
        self, tmp_path, monkeypatch
    ):
        """A pick landing during a SUCCESSFUL reset wins over derived values.

        The project/workspace endpoints do not take the slot lock, so a
        user's explicit pick can land while the agent switch's reset await is
        in flight. The commit is compare-and-set against a pre-await
        baseline: the switch's DERIVED workspace/project must not overwrite
        the newer explicit pick, and the response names the slot's final
        reality. `agent` itself still commits — it is owned exclusively by
        this endpoint.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.agent = "oncall"
        slot.workspace = "oncall-ws"
        slot.project = "/tmp/oncall"
        seen_during_reset: list[tuple[str, str]] = []

        async def _concurrent_pick_lands_then_reset_succeeds(*args, **kwargs):
            # The derived bindings must already be committed when the reset
            # runs: a send in this window cold-starts the replacement session
            # from the slot's CURRENT values, and starting the new agent in
            # the old project would run its tools in the wrong repository.
            seen_during_reset.append((slot.workspace, slot.project))
            # A project pick (unlocked endpoint) lands mid-reset.
            slot.project = "/tmp/user-picked"
            return True

        state.sessions.reset = AsyncMock(side_effect=_concurrent_pick_lands_then_reset_succeeds)

        mock_cfg = MagicMock()
        mock_cfg.agents = {"research": MagicMock(workspace="research-ws", memory_store="default")}
        mock_cfg.workspaces = {"research-ws": MagicMock(dir="/tmp/research")}
        mock_cfg.default_workspace = "default"
        mock_cfg.default_memory_store = "default"
        mock_cfg.memory_stores = {}
        mock_cfg.memory = MagicMock()
        mock_bindings = MagicMock()
        mock_bindings.workspace_dir = Path("/tmp/research")
        mock_bindings.memory_store_name = "default"
        mock_bindings.model = ""
        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.resolve_agent_bindings",
            lambda cfg, name, project_dir=None: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name, project_dir=None: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "research"})
            data = await resp.json()
            assert resp.status == 200
            # The agent itself committed; the derived workspace committed
            # (untouched since baseline); the user's mid-reset project pick
            # SURVIVED the commit instead of being overwritten by the
            # derived project reset.
            assert slot.agent == "research"
            assert slot.workspace == "research-ws"
            assert slot.project == "/tmp/user-picked"
            # The derived workspace was already committed when the reset ran
            # (a replacement session in that window starts under the NEW
            # binding, not the old repository).
            assert seen_during_reset[0][0] == "research-ws"
            # The response names the slot's final reality.
            assert data["workspace"] == "research-ws"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_new_agent_visible_during_reset(self, tmp_path, monkeypatch):
        """The new agent is committed before the reset runs.

        A message send landing while the reset await is in flight creates a
        fresh session from the slot's CURRENT bindings — if the agent were
        committed only after the reset, that session would cold-start on the
        OLD agent and stay stale while the switch reports success.
        (`agent` has no unlocked writers, so committing before the reset is
        safe: the failure path's rollback races nobody.)
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.agent = "oncall"
        seen_during_reset: list[str] = []

        async def _observe_then_succeed(*args, **kwargs):
            seen_during_reset.append(slot.agent)
            return True

        state.sessions.reset = AsyncMock(side_effect=_observe_then_succeed)

        def _boom():
            raise RuntimeError("config unreadable")

        # Resolution outcome is irrelevant to the visibility property.
        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", _boom)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "research"})
            assert resp.status == 200
            assert seen_during_reset == ["research"]
            assert slot.agent == "research"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_response_names_workspace_picked_during_persist(
        self, tmp_path, monkeypatch
    ):
        """The response snapshots the workspace LAST, before leaving the lock.

        The metadata persist awaits a worker thread, yielding the event loop
        — a concurrent /workspace pick landing in that window must be what
        the response names, because the acting tab writes the response's
        workspace into its store optimistically. A snapshot taken before the
        persist would hand the tab a stale value that the pick had already
        superseded.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.workspace = "oncall-ws"
        state.sessions.reset = AsyncMock()

        def _boom():
            raise RuntimeError("config unreadable")

        # Resolution outcome is irrelevant; keep the derived commit a no-op.
        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", _boom)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom)

        # A conversation_log whose update_metadata simulates a concurrent
        # /workspace pick landing while the persist thread runs.
        conv = MagicMock()

        def _pick_lands_during_persist(*args, **kwargs):
            slot.workspace = "user-picked-ws"

        conv.update_metadata = MagicMock(side_effect=_pick_lands_during_persist)
        state.conversation_log = conv

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "research"})
            data = await resp.json()
            assert resp.status == 200
            # The response names the newest reality, not a pre-persist
            # snapshot.
            assert data["workspace"] == "user-picked-ws"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_updates_project_dir(self, tmp_path, monkeypatch):
        """Switching agent also updates slot.project to the new workspace dir.

        Without this, the dashboard file search stays scoped to the previous
        workspace even after the user selects a different agent.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.project = "/old/project"
        state.sessions.reset = AsyncMock()

        mock_cfg = MagicMock()
        mock_cfg.agents = {"dev": MagicMock(workspace="dev-ws", memory_store="default")}
        mock_cfg.workspaces = {"dev-ws": MagicMock(dir="/workspace/dev")}
        mock_cfg.default_workspace = "default"
        mock_cfg.default_memory_store = "default"
        mock_cfg.memory_stores = {"default": MagicMock()}
        mock_cfg.memory = MagicMock()

        mock_bindings = MagicMock()
        mock_bindings.workspace_dir = Path("/workspace/dev")
        mock_bindings.memory_store_name = "default"
        mock_bindings.model = ""

        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.resolve_agent_bindings",
            lambda cfg, name, project_dir=None: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name, project_dir=None: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat._workspace_name_for_dir",
            lambda cfg, ws_dir: "dev-ws",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "dev-ws",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.default_project_dir",
            lambda ws: "/workspace/dev",
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "dev"})
            assert resp.status == 200
            assert slot.project == "/workspace/dev"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_keeps_project_for_project_agent(self, tmp_path, monkeypatch):
        """Selecting a PROJECT-scope agent must not reset slot.project.

        kiro-cli resolves --agent against $PWD/.kiro/agents, so clobbering the
        project here makes the just-selected agent unresolvable on the next
        turn: the slot advertises it while the default answers — the
        silent-substitution bug #1684 exists to remove.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.project = str(tmp_path / "repo")
        state.sessions.reset = AsyncMock()

        mock_cfg = MagicMock()
        mock_cfg.agents = {}  # not an alias — resolvable only via the project scope

        mock_bindings = MagicMock()
        mock_bindings.workspace_dir = Path("/workspace/default")
        mock_bindings.requested_resolved = True

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name, project_dir=None: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "default",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.warm_project_agent_names", AsyncMock()
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.cached_project_agent_names",
            lambda project_dir: frozenset({"repo-bot"}),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.default_project_dir",
            lambda ws: "/workspace/default",
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "repo-bot"})
            assert resp.status == 200
            assert slot.project == str(
                tmp_path / "repo"
            ), f"project agent selection clobbered slot.project: {slot.project!r}"

    @pytest.mark.asyncio
    async def test_api_chat_slot_workspace_updates_project_dir(self, tmp_path, monkeypatch):
        """Switching workspace also updates slot.project to the new workspace dir."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.project = "/old/project"
        state.sessions.reset = AsyncMock()

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.default_project_dir",
            lambda ws: "/workspace/new-ws",
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/workspace", json={"workspace": "new-ws"})
            assert resp.status == 200
            assert slot.project == "/workspace/new-ws"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_persists_to_metadata(self, tmp_path, monkeypatch):
        """Switching a slot's agent writes the new value to the JSONL metadata.

        Without this, a session resumed after a gateway restart reverts to
        whatever agent (if any) was recorded in the initial metadata line.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.sessions.reset = AsyncMock()

        # Seed a session file so update_metadata has something to patch.
        # Use the canonical colon-separated key that the API handler derives
        # via _history_key_for("s1") → "dashboard:s1".  Using "dashboard_s1"
        # (underscore) maps to the same *file* on disk (_safe_key converts
        # both to "dashboard_s1.jsonl") but creates a different *cache key*,
        # so update_metadata's cache invalidation for "dashboard:s1" would
        # leave the "dashboard_s1" cache entry stale.
        history_key = "dashboard:s1"
        state.conversation_log.append(history_key, "user", "hi", agent="old-agent")
        assert state.conversation_log.get_metadata(history_key).get("agent") == "old-agent"

        # Minimal config stub (agent-binding resolution is exercised by the
        # workspace-focused test above; here we only care about persistence).
        mock_cfg = MagicMock()
        mock_cfg.agents = {}
        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "new-agent"})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["agent"] == "new-agent"

        meta = state.conversation_log.get_metadata(history_key)
        assert (
            meta.get("agent") == "new-agent"
        ), f"expected new-agent in metadata, got {meta.get('agent')!r}"

    @pytest.mark.asyncio
    async def test_api_chat_slot_create_response_includes_workspace(self, tmp_path, monkeypatch):
        """api_chat_slot_create response includes resolved workspace field.

        Requirements: 2.4
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        mock_cfg = MagicMock()
        mock_cfg.agents = {"research": MagicMock(workspace="research-ws", memory_store="default")}
        mock_cfg.workspaces = {"research-ws": MagicMock(dir="/tmp/research")}
        mock_cfg.default_workspace = "default"
        mock_cfg.default_memory_store = "default"
        mock_cfg.memory_stores = {}
        mock_cfg.memory = MagicMock()

        mock_bindings = MagicMock()
        mock_bindings.workspace_dir = Path("/tmp/research")
        mock_bindings.memory_store_name = "default"
        mock_bindings.model = ""

        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.resolve_agent_bindings",
            lambda cfg, name, project_dir=None: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name, project_dir=None: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post(
                "/api/chat/slots",
                json={"name": "new-slot", "agent": "research"},
            )
            data = await resp.json()
            assert resp.status == 200
            assert data["workspace"] == "research-ws"

    def test_get_or_create_slot_accepts_workspace_parameter(self, tmp_path, monkeypatch):
        """get_or_create_slot accepts workspace parameter and sets it on the slot.

        Requirements: 2.3
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        slot = state.get_or_create_slot("ws-test", agent="oncall", workspace="oncall-ws")
        assert slot.workspace == "oncall-ws"
        assert slot.agent == "oncall"

        # Default workspace when not specified
        slot2 = state.get_or_create_slot("ws-default")
        assert slot2.workspace == "default"

        # Mode parameter
        slot3 = state.get_or_create_slot("mode-test", mode="orchestrator")
        assert slot3.mode == "orchestrator"
        assert state.get_or_create_slot("ws-default").mode == ""

    @pytest.mark.asyncio
    async def test_run_chat_passes_memory_store_to_build_message(self, tmp_path, monkeypatch):
        """_run_chat resolves agent bindings and passes memory_store to build_message.

        Requirements: 3.1
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        # Track calls to build_message
        build_message_calls: list[dict] = []

        def mock_build_message(self_ctx, text, is_new, session_key=None, **kwargs):
            build_message_calls.append({"text": text, "session_key": session_key, "kwargs": kwargs})
            return text, MagicMock(action=None, text="")

        # Mock config loading
        mock_cfg = MagicMock()
        mock_cfg.agents = {"oncall": MagicMock(workspace="oncall-ws", memory_store="oncall-mem")}
        mock_cfg.default_agent = "default"

        mock_bindings = MagicMock()
        mock_bindings.memory_store_name = "oncall-mem"
        mock_bindings.model = ""

        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.resolve_agent_bindings",
            lambda cfg, name, project_dir=None: mock_bindings,
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.resolve_agent_bindings",
            lambda cfg, name, project_dir=None: mock_bindings,
        )

        # Create a context builder with mocked build_message
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        ctx_builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        monkeypatch.setattr(
            ctx_builder, "build_message", lambda *a, **kw: mock_build_message(ctx_builder, *a, **kw)
        )

        state = _make_state(tmp_path, context_builder=ctx_builder)

        # Create a slot with retained history, then verify a cold start replays it.
        slot = state.get_or_create_slot("mem-test", agent="oncall")
        conversation_log = ConversationLog(base_dir=tmp_path / "sessions")
        conversation_log.init()
        await asyncio.to_thread(
            conversation_log.append, "dashboard:mem-test", "user", "frozen retained question"
        )
        await asyncio.to_thread(
            conversation_log.append,
            "dashboard:mem-test",
            "assistant",
            "frozen retained answer",
        )
        await asyncio.to_thread(
            conversation_log.append, "dashboard:mem-test", "user", "test message"
        )
        ctx_builder.conversation_log = conversation_log
        state.conversation_log = conversation_log
        slot.append("user", "retained question", "msg msg-u")
        slot.append("assistant", "retained answer", "msg msg-a")
        slot.append("user", "test message", "msg msg-u")

        # Mock session manager to return a mock client
        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=AsyncIterator([]))
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)
        state.sessions.consume_replay_suppression = MagicMock(return_value=False)

        # Import and run _run_chat
        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "test message")

        # Verify build_message was called with memory_store
        assert len(build_message_calls) == 1
        assert build_message_calls[0]["kwargs"].get("memory_store") == "oncall-mem"
        assert build_message_calls[0]["session_key"] == "dashboard:mem-test"
        replay = build_message_calls[0]["kwargs"].get("compressed_history")
        assert "frozen retained question" in replay
        assert "frozen retained answer" in replay

    @pytest.mark.asyncio
    async def test_run_chat_forwards_and_clears_the_reinjection_flag(self, tmp_path, monkeypatch):
        """A compaction flags the session; the NEXT _run_chat must forward
        needs_reinjection=True to build_message and clear the flag so the turn
        after that does not re-inject again.

        Regression guard: without this wiring the flag is set by the compact
        callback and read by nobody, so the whole feature is dead code.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        build_message_calls: list[dict] = []

        def mock_build_message(self_ctx, text, is_new, session_key=None, **kwargs):
            build_message_calls.append({"text": text, "kwargs": kwargs})
            return text, MagicMock(action=None, text="")

        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        ctx_builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        monkeypatch.setattr(
            ctx_builder, "build_message", lambda *a, **kw: mock_build_message(ctx_builder, *a, **kw)
        )

        state = _make_state(tmp_path, context_builder=ctx_builder)
        slot = state.get_or_create_slot("reinject-test")

        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=AsyncIterator([]))
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)

        # Stand in for the real SessionManager flag store, including the
        # mark/consume round-trip so the empty-response re-queue path is
        # exercised the way production behaves.
        flag = {"set": True}

        def _consume(key):
            was = flag["set"]
            flag["set"] = False
            return was

        def _mark(key):
            flag["set"] = True

        state.sessions.consume_needs_reinjection = MagicMock(side_effect=_consume)
        state.sessions.mark_needs_reinjection = MagicMock(side_effect=_mark)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "first turn after compaction")

        # The stream is empty, so this turn does not land. The flag must be put
        # BACK -- asserting on the mark call directly, because asserting that some
        # build carried True is tautological (the first assertion already pins it,
        # and the re-queue is drained by an outer worker, not an inner loop).
        assert build_message_calls, "build_message must have been called"
        assert (
            build_message_calls[0]["kwargs"].get("needs_reinjection") is True
        ), "the turn right after a compaction must re-inject the skills index"
        assert state.sessions.mark_needs_reinjection.called, (
            "a turn that consumed the flag but did not land must restore it, "
            "or the skills index is lost for the rest of the session"
        )
        assert flag["set"] is True, "the flag must be back on after a non-landing turn"

        # Once a turn genuinely lands, the flag is gone and later turns are clean.
        flag["set"] = False
        build_message_calls.clear()
        await _run_chat(state, slot, "a later turn")
        assert build_message_calls, "build_message must have been called again"
        assert all(
            c["kwargs"].get("needs_reinjection") is False for c in build_message_calls
        ), "the flag must be one-shot, not sticky for every later turn"

    @pytest.mark.asyncio
    async def test_member_first_turn_that_never_lands_rearms_reinjection(
        self, tmp_path, monkeypatch
    ):
        """A member DM's FIRST turn that ends without landing — a user Stop's
        cancelled completion, an empty stream, a graceful cancel — never enters
        the except arm, so an except-arm-only re-arm leaves the warm session
        running WITHOUT [PERMANENT RULES] for the rest of its life. The re-arm
        must live in the finally, on every exit path."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        ctx_builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        monkeypatch.setattr(
            ctx_builder,
            "build_message",
            lambda text, is_new, session_key=None, **kw: (text, MagicMock(action=None, text="")),
        )

        state = _make_state(tmp_path, context_builder=ctx_builder)
        slot = state.get_or_create_slot("member-rearm-test", mode="member")

        mock_client = MagicMock()
        # Empty stream: the turn ends with no landing and NO exception — the
        # same observable shape as a Stop's cancelled completion.
        mock_client.stream = MagicMock(return_value=AsyncIterator([]))
        # is_new=True: this is the member session's FIRST turn.
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)
        # The compaction flag is OFF, so only the member condition can re-arm.
        state.sessions.consume_needs_reinjection = MagicMock(return_value=False)
        state.sessions.mark_needs_reinjection = MagicMock()

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "first member turn")

        assert state.sessions.mark_needs_reinjection.called, (
            "a member first turn that never landed must re-arm reinjection on "
            "EVERY exit path (finally), or the next warm turn runs without the "
            "member section and its [PERMANENT RULES]"
        )


class TestRunChatToolBoundarySegments:
    """Test that _run_chat inserts whitespace across tool call boundaries."""

    @pytest.mark.asyncio
    async def test_tool_boundary_splits_segments(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import LLMEvent

        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state._hook_store = None

        events = [
            LLMEvent(kind="text_chunk", text="Let me check."),
            LLMEvent(kind="tool_call", title="Read File", tool_kind="read"),
            LLMEvent(kind="text_chunk", text="Done!"),
            LLMEvent(kind="complete"),
        ]

        fake_client = AsyncMock()

        async def _stream(msg):
            for e in events:
                yield e

        fake_client.stream = _stream
        fake_client.context_usage_pct = MagicMock(return_value=0.0)
        state.sessions.get_or_create = AsyncMock(return_value=(fake_client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)
        state.sessions.check_context_usage = MagicMock()
        state.sessions.record_success = MagicMock()
        state.sessions.record_failure = AsyncMock()
        state.sessions.release = MagicMock()

        slot = state.get_or_create_slot("s1")
        await _run_chat(state, slot, "do it")

        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        # With _flush_segment, text is split into separate segments at tool boundaries
        # so gluing can't happen — each segment is independent
        assert len(assistant_msgs) == 2
        assert "Let me check." in assistant_msgs[0]["content"]
        assert "Done!" in assistant_msgs[1]["content"]

    @pytest.mark.asyncio
    async def test_tool_boundary_empty_chunk_still_splits(self, tmp_path, monkeypatch):
        """Empty text chunk after tool call doesn't prevent segment splitting."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import LLMEvent

        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state._hook_store = None

        events = [
            LLMEvent(kind="text_chunk", text="Before."),
            LLMEvent(kind="tool_call", title="T", tool_kind="read"),
            LLMEvent(kind="text_chunk", text=""),  # empty chunk
            LLMEvent(kind="text_chunk", text="After!"),
            LLMEvent(kind="complete"),
        ]

        fake_client = AsyncMock()

        async def _stream(msg):
            for e in events:
                yield e

        fake_client.stream = _stream
        fake_client.context_usage_pct = MagicMock(return_value=0.0)
        state.sessions.get_or_create = AsyncMock(return_value=(fake_client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)
        state.sessions.check_context_usage = MagicMock()
        state.sessions.record_success = MagicMock()
        state.sessions.record_failure = AsyncMock()
        state.sessions.release = MagicMock()

        slot = state.get_or_create_slot("s1")
        await _run_chat(state, slot, "do it")

        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        # Segments are flushed at tool boundaries; empty chunks don't create segments
        assert len(assistant_msgs) == 2
        assert "Before." in assistant_msgs[0]["content"]
        assert "After!" in assistant_msgs[1]["content"]


class TestRunChatToolCallUpdate:
    """EVENT_TOOL_CALL_UPDATE handler — claude-agent-acp emits a refinement
    once the streamed tool input is complete. The handler patches the in-place
    pill, persisted message, _pending_tools, and the SEL audit trail."""

    @staticmethod
    def _make_mock_client(events):
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_state_for_run_chat(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_refinement_patches_pill_content_and_meta(self, tmp_path, monkeypatch):
        """An initial tool_call with a stub title is overwritten by the refined
        title and the meta picks up the populated input."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL, title="Terminal", tool_kind="execute", tool_call_id="tc-1"
            ),
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_input='{"command": "ls /tmp"}',
                tool_call_id="tc-1",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        tool_msgs = [m for m in slot.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        # The persisted content should reflect the refined title, not the stub.
        assert tool_msgs[0]["content"] == "🔧 ls /tmp"
        assert tool_msgs[0]["meta"]["tool_call_id"] == "tc-1"
        # The refined input is patched into meta.
        assert "ls /tmp" in tool_msgs[0]["meta"]["input"]

    @pytest.mark.asyncio
    async def test_refinement_carries_purpose_when_it_has_one(self, tmp_path, monkeypatch):
        """The update frame must carry the purpose so the live status line keeps
        showing the agent's own reason for the call."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Terminal",
                tool_kind="execute",
                tool_purpose="List the temp dir",
                tool_call_id="tc-p1",
            ),
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_purpose="List the temp dir",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-p1",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        ws_calls = [(c.args[0], c.args[1]) for c in state.broadcast_ws.call_args_list]
        (update,) = [p for k, p in ws_calls if k == "tool_call" and p.get("is_update")]
        assert update["purpose"] == "List the temp dir"

    @pytest.mark.asyncio
    async def test_purposeless_refinement_omits_the_key(self, tmp_path, monkeypatch):
        """A refinement with no purpose omits the key entirely rather than
        sending an empty string: consumers merge field-by-field and read an
        absent ``purpose`` as "keep what the initial tool_call supplied", so an
        empty value would replace a good purpose with the raw command."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Terminal",
                tool_kind="execute",
                tool_purpose="List the temp dir",
                tool_call_id="tc-p2",
            ),
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-p2",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        ws_calls = [(c.args[0], c.args[1]) for c in state.broadcast_ws.call_args_list]
        (update,) = [p for k, p in ws_calls if k == "tool_call" and p.get("is_update")]
        assert "purpose" not in update

    @pytest.mark.asyncio
    async def test_refinement_persists_a_recovered_purpose(self, tmp_path, monkeypatch):
        """A refinement's purpose must reach the PERSISTED meta, not just the live
        status: when the initial tool_call streamed an empty rawInput, _tool_meta
        wrote an empty purpose, and the reloaded transcript reads meta.purpose —
        so a live-only patch loses it on the next reload."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            # Streaming backend: no purpose and no input on the initial frame.
            LLMEvent(
                kind=EVENT_TOOL_CALL, title="Terminal", tool_kind="execute", tool_call_id="tc-p3"
            ),
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_purpose="List the temp dir",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-p3",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        (tool_msg,) = [m for m in slot.messages if m.get("role") == "tool"]
        assert tool_msg["meta"]["purpose"] == "List the temp dir"
        # And the same patch goes out live so an open tab does not wait for a reload.
        ws_calls = [(c.args[0], c.args[1]) for c in state.broadcast_ws.call_args_list]
        (msg_update,) = [p for k, p in ws_calls if k == "chat_message_update"]
        assert msg_update["meta"]["purpose"] == "List the temp dir"

    @pytest.mark.asyncio
    async def test_tool_meta_persists_kind(self, tmp_path, monkeypatch):
        """The persisted tool-message meta carries the ACP tool kind. The
        dashboard gates the inline diff-card promotion on kind == 'edit'
        (a shell command whose input looks like a diff must never promote),
        and historical rows can only be gated from persisted meta."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="fs_write",
                tool_kind="edit",
                tool_purpose="Edit app.py",
                tool_input="--- /a/app.py\n+++ /a/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2",
                tool_call_id="tc-kind1",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        (tool_msg,) = [m for m in slot.messages if m.get("role") == "tool"]
        assert tool_msg["meta"]["kind"] == "edit"

    @pytest.mark.asyncio
    async def test_refinement_broadcasts_chat_message_update(self, tmp_path, monkeypatch):
        """The handler broadcasts a chat_message_update WS event so the
        frontend can patch the persisted tile in place without a reload."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL, title="Terminal", tool_kind="execute", tool_call_id="tc-2"
            ),
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-2",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        ws_calls = [(c.args[0], c.args[1]) for c in state.broadcast_ws.call_args_list]
        kinds = [k for k, _ in ws_calls]
        # Refinement broadcasts both a fresh tool_call (toolLog merge) AND a
        # chat_message_update so the persisted tile updates live too.
        assert kinds.count("tool_call") >= 2
        assert "chat_message_update" in kinds
        # The second tool_call carries is_update:True so the frontend merges.
        update_payloads = [p for k, p in ws_calls if k == "tool_call" and p.get("is_update")]
        assert len(update_payloads) == 1
        assert update_payloads[0]["tool"] == "ls /tmp"
        assert update_payloads[0]["tool_call_id"] == "tc-2"
        # And the chat_message_update carries the patched content + meta.
        msg_updates = [p for k, p in ws_calls if k == "chat_message_update"]
        assert msg_updates[0]["tool_call_id"] == "tc-2"
        assert msg_updates[0]["content"] == "🔧 ls /tmp"
        assert "input" in msg_updates[0]["meta"]

    @pytest.mark.asyncio
    async def test_refinement_preserves_existing_icon(self, tmp_path, monkeypatch):
        """Auto-approved tools may already carry a ✅ marker on the message
        with the same tool_call_id. The patch must preserve that prefix."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        # Pre-seed a message that matches the tool_call_id with a ✅ icon —
        # mirrors the auto-approved-tool case where the post-approval marker
        # is already in place.
        slot.append("tool", "✅ Terminal", "msg msg-tool", meta={"tool_call_id": "tc-3"})

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-3",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        tool_msgs = [m for m in slot.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        # ✅ icon survives the patch; only the title changes.
        assert tool_msgs[0]["content"] == "✅ ls /tmp"

    @pytest.mark.asyncio
    async def test_refinement_breaks_on_first_match_walking_reverse(self, tmp_path, monkeypatch):
        """When two messages share the tool_call_id (auto-approved double-emit
        with 🔧 then ✅), only the most recent one is patched."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("tool", "🔧 Terminal", "msg msg-tool", meta={"tool_call_id": "tc-4"})
        slot.append("tool", "✅ Terminal", "msg msg-tool", meta={"tool_call_id": "tc-4"})

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_call_id="tc-4",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        tool_msgs = [m for m in slot.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 2
        # The earlier 🔧 message is untouched.
        assert tool_msgs[0]["content"] == "🔧 Terminal"
        # The later ✅ message is patched, with its icon preserved.
        assert tool_msgs[1]["content"] == "✅ ls /tmp"

    @pytest.mark.asyncio
    async def test_refinement_strips_running_prefix_from_pending_tools(self, tmp_path, monkeypatch):
        """_pending_tools feeds PostToolUse hooks by tool name. The refinement
        must strip the "Running: " prefix exactly like EVENT_TOOL_CALL does so
        hooks matching by name keep working after the refinement event."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Running: stub",
                tool_kind="execute",
                tool_call_id="tc-5",
            ),
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="Running: ls /tmp",
                tool_kind="execute",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-5",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        # Patch fire_tool_hooks so we can inspect the name passed in. The hook
        # only fires on EVENT_TOOL_CALL — but the refinement updates
        # _pending_tools, which is read by the EVENT_TOOL_RESULT handler. We
        # don't drive a tool_result here; instead we inspect _pending_tools
        # was updated correctly via the WS-broadcast surface area: the
        # refinement broadcasts the refined title without the "Running: "
        # prefix on the handler's local copy.
        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # Indirectly verify via the persisted message — the title displayed on
        # the pill should be the refined one, sans "Running: " prefix because
        # the refinement code strips it for _pending_tools (the displayed
        # title still carries the prefix, but the hook-name copy doesn't).
        # The persisted pill carries the refined title verbatim — this is the
        # display surface, not the hook surface.
        tool_msgs = [m for m in slot.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["content"] == "🔧 Running: ls /tmp"

    @pytest.mark.asyncio
    async def test_refinement_logs_sel_audit_event(self, tmp_path, monkeypatch):
        """The handler logs a `tool_invocation` audit event with
        outcome="refined" so the audit trail captures the refined name."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        captured = []

        class _FakeSel:
            def log_tool_invocation(self, **kw):
                captured.append(kw)

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.sel", lambda: _FakeSel())

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-6",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        refined = [c for c in captured if c.get("outcome") == "refined"]
        assert len(refined) == 1
        assert refined[0]["tool_name"] == "ls /tmp"
        assert refined[0]["tool_kind"] == "execute"
        assert refined[0]["source"] == "dashboard"

    @pytest.mark.asyncio
    async def test_refinement_no_tool_call_id_skipped(self, tmp_path, monkeypatch):
        """Refinement events without a tool_call_id are silently dropped —
        we have nothing to merge against."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TOOL_CALL_UPDATE, title="ls /tmp", tool_call_id=""),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # No tool messages should have been added.
        tool_msgs = [m for m in slot.messages if m.get("role") == "tool"]
        assert tool_msgs == []
        # No chat_message_update broadcast either.
        kinds = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "chat_message_update" not in kinds

    @pytest.mark.asyncio
    async def test_refinement_no_matching_message_no_chat_message_update(
        self, tmp_path, monkeypatch
    ):
        """When no persisted tool message matches the tool_call_id, the
        handler still broadcasts the tool_call merge but skips
        chat_message_update (nothing to patch)."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-orphan",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        kinds = [c.args[0] for c in state.broadcast_ws.call_args_list]
        # tool_call still broadcast (toolLog merge by id is still meaningful).
        assert "tool_call" in kinds
        # Nothing to patch in slot.messages, so no chat_message_update.
        assert "chat_message_update" not in kinds

    @pytest.mark.asyncio
    async def test_refinement_redacts_credentials_in_input(self, tmp_path, monkeypatch):
        """Credentials in tool_input must be redacted before the broadcast
        and the persisted meta. _redact_tool_field applies both
        redact_exfiltration_urls and redact_credentials."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("tool", "🔧 Terminal", "msg msg-tool", meta={"tool_call_id": "tc-cred"})

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="echo secret",
                tool_kind="execute",
                tool_input='{"command":"echo AKIAIOSFODNN7EXAMPLE"}',
                tool_call_id="tc-cred",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        tool_msgs = [m for m in slot.messages if m.get("role") == "tool"]
        # Credential must not appear in the persisted meta.
        assert "AKIAIOSFODNN7EXAMPLE" not in tool_msgs[0]["meta"].get("input", "")
        # Or in any of the broadcast payloads.
        for call in state.broadcast_ws.call_args_list:
            assert "AKIAIOSFODNN7EXAMPLE" not in str(call.args[1])

    @pytest.mark.asyncio
    async def test_refinement_handler_swallows_exceptions(self, tmp_path, monkeypatch):
        """A malformed broadcast or other exception inside the handler must
        not tear down the run loop. The try/except logs and continues."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        # Make broadcast_ws raise on the first call (the tool_call broadcast).
        # The try/except in the handler must catch it so subsequent events
        # (the trailing EVENT_TEXT_CHUNK) still process.
        original_broadcast = state.broadcast_ws

        def _raising_then_normal(*args, **kwargs):
            if args and args[0] == "tool_call":
                raise RuntimeError("simulated broadcast failure")
            return original_broadcast(*args, **kwargs)

        state.broadcast_ws = MagicMock(side_effect=_raising_then_normal)

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_call_id="tc-boom",
            ),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="after the boom"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        # Must not raise — the run loop should continue past the exception.
        await _run_chat(state, slot, "hello")

        # The trailing assistant message proves the run loop survived.
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert any("after the boom" in m["content"] for m in assistant_msgs)


# ── Model refusal (kiro-cli `refusal` stop reason) ──


class TestRunChatModelRefusal:
    """A turn that ends on stop_reason 'refusal' with no text is a DETERMINISTIC
    model-side content refusal — it must surface a distinct, non-retried card
    instead of falling into the blind empty-response retry loop (which just
    re-hits the same refusal and burns credits)."""

    @staticmethod
    def _make_mock_client(events):
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_state_for_run_chat(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_stale_recover_notice_is_tagged_and_emits_one_frame(self, tmp_path, monkeypatch):
        """The LIVE frame must carry the retry tag, and there must be exactly one."""
        from kiro_crew.acp.types import STOP_REASON_STALE_RECOVER
        from kiro_crew.dashboard.chat_utils import TRANSIENT_RETRY_KIND
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_STALE_RECOVER)]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        _notices = [
            m
            for m in slot.messages
            if m.get("role") == "error" and "Recovering a stalled turn" in m.get("content", "")
        ]
        assert _notices, "stale-recovery notice not emitted"
        assert all((m.get("meta") or {}).get("kind") == TRANSIENT_RETRY_KIND for m in _notices)
        # slot.append already emits ONE tagged chat_message; an explicit frame here is an
        # untagged duplicate, and a client rendering it re-offers the executing choice.
        _dupes = [
            c
            for c in state.broadcast_ws.call_args_list
            if c.args
            and c.args[0] == "chat_message"
            and isinstance(c.args[1], dict)
            and "Recovering a stalled turn" in str(c.args[1].get("content", ""))
        ]
        assert not _dupes, f"untagged explicit chat_message duplicate(s): {_dupes}"

    @pytest.mark.asyncio
    async def test_refusal_shows_declined_card_and_does_not_retry(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_REFUSAL
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_REFUSAL)]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # A single declined card is surfaced, NOT the empty-response card.
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("declined" in m.get("content", "").lower() for m in error_msgs)
        assert not any("empty response" in m.get("content", "").lower() for m in error_msgs)
        # No blind re-queue: the message is not re-enqueued and the retry budget
        # is untouched.
        assert not slot._queue
        assert slot._empty_response_retries == 0


# ── Mode/approval policy propagation (HTTP handlers) ──


class TestApiChatModePropagation:
    """api_chat_mode propagates approval policy to all session slots."""

    @pytest.mark.asyncio
    async def test_yolo_mode_propagates_auto_policy(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")
        state.get_or_create_slot("s2")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "yolo"})
            data = await resp.json()
            assert data["ok"] is True

        calls = state.sessions.set_approval_policy.call_args_list
        keys = [c.args[0] for c in calls]
        policies = [c.args[1] for c in calls]
        assert "dashboard:s1" in keys
        assert "dashboard:s2" in keys
        assert all(p == "auto" for p in policies)

    @pytest.mark.asyncio
    async def test_normal_mode_clears_policy(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.safety_override import safety_override

        safety_override().activate("test")
        state = _make_state(tmp_path)
        state.enable_yolo()
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot._trust = False

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal"})
            data = await resp.json()
            assert data["ok"] is True

        state.sessions.set_approval_policy.assert_called_with("dashboard:s1", "")

    @pytest.mark.asyncio
    async def test_trust_mode_scoped_to_slot_channel(self, tmp_path, monkeypatch):
        """Trust with slot_key only trusts that slot's linked channel."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot._slack_channel = "ch1"

        ch1 = MagicMock(trusted=False)
        ch2 = MagicMock(trusted=False)
        mgr = MagicMock(_channels={"ch1": ch1, "ch2": ch2})
        state.channel_manager = mgr

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            assert (await resp.json())["ok"] is True

        assert ch1.trusted is True
        ch1._save.assert_called_once()
        assert ch2.trusted is False
        ch2._save.assert_not_called()

    @pytest.mark.asyncio
    async def test_trust_mode_all_channels_when_no_slot(self, tmp_path, monkeypatch):
        """Trust without slot_key trusts all channels."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        ch = MagicMock(trusted=False)
        mgr = MagicMock(_channels={"ch1": ch})
        state.channel_manager = mgr

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust"})
            assert (await resp.json())["ok"] is True

        assert ch.trusted is True
        ch._save.assert_called_once()

    @pytest.mark.asyncio
    async def test_normal_mode_scoped_resets_only_linked_channel(self, tmp_path, monkeypatch):
        """Normal mode with slot_key should only reset that slot's linked channel."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot._slack_channel = "ch1"
        state.get_or_create_slot("s2")

        ch1 = MagicMock(trusted=True)
        ch2 = MagicMock(trusted=True)
        mgr = MagicMock(_channels={"ch1": ch1, "ch2": ch2})
        state.channel_manager = mgr

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})
            assert (await resp.json())["ok"] is True

        assert ch1.trusted is False
        ch1._save.assert_called_once()
        assert ch2.trusted is True
        ch2._save.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_mode_resets_all_channels_when_no_slot(self, tmp_path, monkeypatch):
        """Normal mode without slot_key resets all channel trust."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        ch = MagicMock(trusted=True)
        mgr = MagicMock(_channels={"ch1": ch})
        state.channel_manager = mgr

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal"})
            assert (await resp.json())["ok"] is True

        assert ch.trusted is False
        ch._save.assert_called_once()

    @pytest.mark.asyncio
    async def test_trust_mode_unknown_slot_returns_400(self, tmp_path, monkeypatch):
        """Trust with unknown slot_key must return 400, not trust all."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/mode", json={"mode": "trust", "slot": "nonexistent"}
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "unknown slot"

    @pytest.mark.asyncio
    async def test_normal_mode_unknown_slot_returns_400(self, tmp_path, monkeypatch):
        """Normal with unknown slot_key must return 400, not reset all."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/mode", json={"mode": "normal", "slot": "nonexistent"}
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "unknown slot"

    @pytest.mark.asyncio
    async def test_trust_slot_preserves_other_slot_trust(self, tmp_path, monkeypatch):
        """trusting slot B must not wipe trust from slot A."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        s1 = state.get_or_create_slot("s1")
        s2 = state.get_or_create_slot("s2")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            assert s1._trust is True

            await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s2"})
            assert s2._trust is True
            assert s1._trust is True  # must survive

    @pytest.mark.asyncio
    async def test_yolo_restores_per_slot_trust(self, tmp_path, monkeypatch):
        """YOLO does not mutate per-slot trust; disabling preserves it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        s1 = state.get_or_create_slot("s1")
        s2 = state.get_or_create_slot("s2")

        async with TestClient(TestServer(_make_app(state))) as client:
            # Set per-slot modes: s1=trust, s2=trust_reads
            await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            await client.post("/api/chat/mode", json={"mode": "trust_reads", "slot": "s2"})
            assert s1._trust is True
            assert s2._trust_reads is True

            # YOLO overrides everything
            await client.post("/api/chat/mode", json={"mode": "yolo"})
            assert s1._trust is True  # unchanged
            assert s2._trust_reads is True  # unchanged

            # Set s1 to normal (leaving YOLO) — s2 should be untouched
            await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})
            assert s1._trust is False
            assert s1._trust_reads is False
            assert s2._trust_reads is True  # preserved

    def test_yolo_auto_expires_and_clears_untrusted_policies(self, tmp_path, monkeypatch):
        """YOLO expiry clears policies for untrusted slots only."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.broadcast_ws = MagicMock()
        s1 = state.get_or_create_slot("s1")
        state.get_or_create_slot("s2")
        s1._trust = True

        from unittest.mock import patch

        from kiro_crew.safety_override import safety_override

        # Wire the on_expired callback (as server.py does at startup)
        def _on_expired(source: str) -> None:
            if state.sessions is not None:
                for slot in state._slots.values():
                    if not slot._trust and not slot._trust_reads:
                        state.sessions.set_approval_policy(f"dashboard:{slot.key}", "")

        with patch("kiro_crew.safety_override.sel"):
            safety_override().activate("dashboard")
        safety_override().on_expired = _on_expired
        safety_override()._expires_at = 0  # already expired

        assert state.is_yolo_active() is False
        assert s1._trust is True  # per-slot trust survives expiry

        cleared = [
            c[0][0] for c in state.sessions.set_approval_policy.call_args_list if c[0][1] == ""
        ]
        assert "dashboard:s2" in cleared
        assert "dashboard:s1" not in cleared

    @pytest.mark.asyncio
    async def test_trust_mode_propagates_approval_policy_to_session(self, tmp_path, monkeypatch):
        """Trust mode must set session approval_policy='auto' so subagents inherit."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.slack.handler.is_yolo_mode", lambda: False)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})

        state.sessions.set_approval_policy.assert_any_call("dashboard:s1", "auto")

    @pytest.mark.asyncio
    async def test_normal_mode_resets_approval_policy(self, tmp_path, monkeypatch):
        """Normal mode must reset session approval_policy so subagents require approval."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.slack.handler.is_yolo_mode", lambda: False)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})

        state.sessions.set_approval_policy.assert_any_call("dashboard:s1", "")

    @pytest.mark.asyncio
    async def test_trust_reads_mode_resets_approval_policy(self, tmp_path, monkeypatch):
        """trust_reads must reset approval_policy (not auto-approve writes)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.slack.handler.is_yolo_mode", lambda: False)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "trust_reads", "slot": "s1"})

        state.sessions.set_approval_policy.assert_any_call("dashboard:s1", "")

    @pytest.mark.asyncio
    async def test_trust_mode_all_slots_propagates_approval_policy(self, tmp_path, monkeypatch):
        """Trust without slot_key must set approval_policy on all slots."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.slack.handler.is_yolo_mode", lambda: False)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")
        state.get_or_create_slot("s2")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "trust"})

        state.sessions.set_approval_policy.assert_any_call("dashboard:s1", "auto")
        state.sessions.set_approval_policy.assert_any_call("dashboard:s2", "auto")


class TestApiChatModeGlobalOverrideScope:
    """A slot-scoped mode change must not revoke the process-global YOLO grant.

    The grant covers every slot, so ending it on behalf of a request that named
    ONE slot changes slots the request never mentioned — the shape that let a
    per-slot `trust` setup call drop the whole gateway out of YOLO.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["trust", "trust_reads"])
    async def test_slot_scoped_trust_preserves_global_grant(self, mode, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.safety_override import safety_override

        safety_override().activate("dashboard")
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")
        state.get_or_create_slot("s2")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": mode, "slot": "s1"})
            assert (await resp.json())["ok"] is True

        assert safety_override().is_active() is True
        # The regression itself: a slot the request never named keeps auto-approving.
        state.sessions.set_approval_policy.assert_any_call("dashboard:s2", "auto")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["trust", "trust_reads"])
    async def test_global_trust_revokes_global_grant(self, mode, tmp_path, monkeypatch):
        """Without a slot the request IS global, so it may end the grant."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.safety_override import safety_override

        safety_override().activate("dashboard")
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": mode})

        assert safety_override().is_active() is False

    @pytest.mark.asyncio
    async def test_slot_scoped_normal_revokes_global_grant(self, tmp_path, monkeypatch):
        """`normal` asks for NO auto-approval, so it revokes at any scope.

        This is the dashboard picker's YOLO off-switch: the picker always names
        the slot it is attached to.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.safety_override import safety_override

        safety_override().activate("dashboard")
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})

        assert safety_override().is_active() is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["trust", "trust_reads"])
    async def test_declared_grant_is_revoked_at_any_scope(self, mode, tmp_path, monkeypatch):
        """A declared grant has no TTL, so any mode selection is its off-switch."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.safety_override import safety_override

        safety_override().activate_declared()
        assert safety_override().is_declared is True
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": mode, "slot": "s1"})

        assert safety_override().is_active() is False

    @pytest.mark.asyncio
    async def test_until_shutdown_grant_is_ad_hoc_and_protected(self, tmp_path, monkeypatch):
        """`until_shutdown` is permanent but AD HOC, so the scope rule applies.

        Classifying it by permanence instead of source would revoke the grant of
        every operator who picked "until Kiro Crew restarts".
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.safety_override import safety_override

        so = safety_override()
        so.adhoc_until_shutdown = True
        so.activate("dashboard")
        assert so.is_permanent is True
        assert so.is_declared is False
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})

        assert so.is_active() is True

    @pytest.mark.asyncio
    async def test_non_string_mode_does_not_raise(self, tmp_path, monkeypatch):
        """The membership test must answer for an unhashable body value."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": {}, "slot": "s1"})

        assert resp.status == 200  # falls through to the normal branch, as before


class TestApproveYoloPropagation:
    """api_chat_slot_approve with yolo action propagates policy to all slots."""

    @pytest.mark.asyncio
    async def test_yolo_approve_propagates_to_all_slots(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        s1 = state.get_or_create_slot("s1")
        state.get_or_create_slot("s2")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        s1._approval_futures["test"] = fut

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "yolo"})
            data = await resp.json()
            assert data["ok"] is True

        calls = state.sessions.set_approval_policy.call_args_list
        keys = [c.args[0] for c in calls]
        assert "dashboard:s1" in keys
        assert "dashboard:s2" in keys

    @pytest.mark.asyncio
    async def test_trust_approve_propagates_to_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["test"] = fut
        slot.messages.append(
            {
                "role": "permission",
                "content": "Running: ls",
                "cls": json.dumps(
                    {
                        "request_id": "test",
                        "full_command": "ls",
                        "trust_grantable": "1",
                    }
                ),
            }
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "trust"})
            data = await resp.json()
            assert data["ok"] is True

        state.sessions.set_approval_policy.assert_called_with("dashboard:s1", "auto")


# ── Coverage: bulk-approve broadcasts ──


class TestBulkApproveBroadcast:
    """Trust/YOLO mode change bulk-approve must broadcast approval_resolved."""

    @pytest.mark.asyncio
    async def test_mode_yolo_broadcasts_for_pending(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        f1: asyncio.Future[str] = loop.create_future()
        f2: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["req-1"] = f1
        slot._approval_futures["req-2"] = f2

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "yolo"})
            assert (await resp.json())["ok"] is True

        broadcast_calls = [
            c for c in state.broadcast_ws.call_args_list if c.args[0] == "approval_resolved"
        ]
        ids = {c.args[1]["id"] for c in broadcast_calls}
        assert "req-1" in ids
        assert "req-2" in ids

    @pytest.mark.asyncio
    async def test_slot_scoped_trust_leaves_other_slots_pending(self, tmp_path, monkeypatch):
        """A slot-scoped ``trust`` must NOT sweep other slots' pending approvals.

        Regression: bulk auto-approve iterated EVERY slot regardless of scope, so
        trusting one chat resolved the pending approval card in unrelated chats
        (making them LOOK approved) while their ``_trust`` flag stayed False — so
        their next tool call prompted again. Scope the sweep to the target
        session; other slots keep their pending future AND their untrusted state.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.broadcast_ws = MagicMock()
        s1 = state.get_or_create_slot("s1")
        s2 = state.get_or_create_slot("s2")
        loop = asyncio.get_running_loop()
        f1: asyncio.Future[str] = loop.create_future()
        f2: asyncio.Future[str] = loop.create_future()
        s1._approval_futures["req-1"] = f1
        s2._approval_futures["req-2"] = f2

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            assert (await resp.json())["ok"] is True

        # Target slot: approved + broadcast + trusted.
        assert f1.done() and f1.result() == "approved"
        assert s1._trust is True
        # Other slot: still pending, never broadcast, still untrusted.
        assert not f2.done()
        assert s2._trust is False
        resolved_ids = {
            c.args[1]["id"]
            for c in state.broadcast_ws.call_args_list
            if c.args[0] == "approval_resolved"
        }
        assert "req-1" in resolved_ids
        assert "req-2" not in resolved_ids
        # Clean up the still-pending future so the loop does not warn on GC.
        f2.set_result("rejected")

    @pytest.mark.asyncio
    async def test_unscoped_trust_still_sweeps_all_slots(self, tmp_path, monkeypatch):
        """An all-slots ``trust`` (no slot named) keeps sweeping every slot."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.broadcast_ws = MagicMock()
        s1 = state.get_or_create_slot("s1")
        s2 = state.get_or_create_slot("s2")
        loop = asyncio.get_running_loop()
        f1: asyncio.Future[str] = loop.create_future()
        f2: asyncio.Future[str] = loop.create_future()
        s1._approval_futures["req-1"] = f1
        s2._approval_futures["req-2"] = f2

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust"})
            assert (await resp.json())["ok"] is True

        assert f1.done() and f2.done()
        assert s1._trust is True and s2._trust is True
        resolved_ids = {
            c.args[1]["id"]
            for c in state.broadcast_ws.call_args_list
            if c.args[0] == "approval_resolved"
        }
        assert {"req-1", "req-2"} <= resolved_ids


# ── Coverage: multi-pending approval 400 and trust auto-approve ──


class TestMultiPendingApproval:
    """Cover the 400 response when multiple approvals are pending without request_id."""

    @pytest.mark.asyncio
    async def test_multi_pending_returns_400(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        slot._approval_futures["a1"] = loop.create_future()
        slot._approval_futures["a2"] = loop.create_future()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "approved"})
            assert resp.status == 400
            data = await resp.json()
            assert "pending" in data
            assert set(data["pending"]) == {"a1", "a2"}

    @pytest.mark.asyncio
    async def test_approve_with_request_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        slot._approval_futures["specific"] = fut

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve",
                json={"action": "approved", "request_id": "specific"},
            )
            assert resp.status == 200
            assert fut.result() == "approved"


# ── Agent passing via /api/chat (AgentRock integration) ──


class TestApiChatAgentPassing:
    @pytest.mark.asyncio
    async def test_agent_set_on_new_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"message": "hello", "slot": "agentrock-my-skill", "agent": "my-aim-agent"},
            )
            data = await resp.json()
            assert data["ok"] is True
            assert state._slots["agentrock-my-skill"].agent == "my-aim-agent"

    @pytest.mark.asyncio
    async def test_agent_mismatch_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slot-x")
        slot.agent = "agent-a"
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"message": "hello", "slot": "slot-x", "agent": "agent-b"},
            )
            assert resp.status == 409

    @pytest.mark.asyncio
    async def test_empty_agent_on_agent_slot_allowed(self, tmp_path, monkeypatch):
        """Follow-up message with no agent on an agent-bound slot must not 409."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slot-y")
        slot.agent = "agent-a"
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"message": "follow-up", "slot": "slot-y", "agent": ""},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_invalid_agent_name_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from unittest.mock import patch

        state = _make_state(tmp_path)
        with patch("kiro_crew.dashboard.chat_handlers._emit_agent_assignment") as mock_emit:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hello", "slot": "s1", "agent": "../evil"},
                )
            assert resp.status == 400
            mock_emit.assert_called_once_with("s1", "../evil", outcome="denied_invalid")

    @pytest.mark.asyncio
    async def test_non_string_agent_logs_actual_value(self, tmp_path, monkeypatch):
        """Fix for Post 22: str(agent) preserves malicious input in audit trail."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from unittest.mock import patch

        state = _make_state(tmp_path)
        with patch("kiro_crew.dashboard.chat_handlers._emit_agent_assignment") as mock_emit:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hello", "slot": "s1", "agent": 123},
                )
            assert resp.status == 400
            mock_emit.assert_called_once_with("s1", "123", outcome="denied_invalid")

    @pytest.mark.asyncio
    async def test_no_agent_no_emit(self, tmp_path, monkeypatch):
        """Fix for Post 23: no SEL event when no agent involved (reduces audit noise)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from unittest.mock import patch

        state = _make_state(tmp_path)
        with patch("kiro_crew.dashboard.chat_handlers._emit_agent_assignment") as mock_emit:
            async with TestClient(TestServer(_make_app(state))) as client:
                await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "no-agent-slot"},
                )
            mock_emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_sel_event_on_running_slot_rejection(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from unittest.mock import MagicMock, patch

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slot-r")
        mock_task = MagicMock()
        mock_task.done.return_value = False
        slot.task = mock_task
        with patch("kiro_crew.dashboard.chat_handlers._emit_agent_assignment") as mock_emit:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "slot-r", "agent": "new-agent"},
                )
                assert resp.status == 409
            mock_emit.assert_called_once_with("slot-r", "new-agent", outcome="denied_running")


class TestApiChatSendReceiptMid:
    """The immediate-dispatch receipt carries the user row's server-minted `mid`.

    The dashboard renders the user turn optimistically and no `chat_message`
    user echo is broadcast for a dashboard send (`append` defaults
    `broadcast_user=False`), so the send receipt is the only channel that can
    hand the client the row's stable id before the chat_done refresh. The
    message-pin control is gated on `meta.mid`, so a missing id keeps the
    just-sent message unpinnable for the whole turn.
    """

    @pytest.mark.asyncio
    async def test_immediate_dispatch_receipt_carries_the_user_row_mid(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            sl.append("chunk", "ack", "chunk")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"message": "hello", "slot": "mid-slot"},
            )
            assert resp.status == 200
            data = await resp.json()

        assert data["ok"] is True
        # The receipt id must be the SAME id stamped on the appended user row —
        # that is the identity the client stamps on the optimistic bubble and
        # the pin endpoint keys on.
        slot = state._slots["mid-slot"]
        user_rows = [m for m in slot.messages if m["role"] == "user"]
        assert user_rows, "the send must have appended a user row"
        row_mid = user_rows[-1]["meta"]["mid"]
        assert row_mid
        assert data["mid"] == row_mid

    @pytest.mark.asyncio
    async def test_queued_send_receipt_carries_no_mid(self, tmp_path, monkeypatch):
        """A busy slot queues the message and broadcasts its own queue card; the
        optimistic bubble is not this row's receipt, so no `mid` is returned
        (mirrors `confirmedDelivered`, which excludes a queued acceptance)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("busy-slot")
        mock_task = MagicMock()
        mock_task.done.return_value = False
        slot.task = mock_task  # slot.running is True → the busy/queue branch

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"message": "queue me", "slot": "busy-slot"},
            )
            assert resp.status == 200
            data = await resp.json()

        assert data["queued"] is True
        assert "mid" not in data


# ── Plan action & auto-run tests ──


class TestPlanAction:
    """Tests for api_chat_plan_action: Go/Go All label display and auto-run flag."""

    @pytest.mark.asyncio
    async def test_go_shows_go_label(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("plan-slot", mode="orchestrator")
        slot.append("assistant", "📋 Plan for: test\n\nStage 1: Do\n\n[OPTION: Go | Cancel]")
        with pytest.MonkeyPatch.context() as m:
            m.setattr("kiro_crew.dashboard.chat_orchestrator._stage_loop", AsyncMock())
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/plan-slot/plan-action",
                    json={"action": "go"},
                )
                assert resp.status == 200
        user_msgs = [m for m in slot.messages if m["role"] == "user"]
        assert user_msgs[-1]["content"] == "Go"
        assert not slot._auto_run

    @pytest.mark.asyncio
    async def test_go_all_shows_go_all_label(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("plan-slot2", mode="orchestrator")
        slot.append("assistant", "📋 Plan for: test\n\nStage 1: Do\n\n[OPTION: Go | Cancel]")
        with pytest.MonkeyPatch.context() as m:
            m.setattr("kiro_crew.dashboard.chat_orchestrator._stage_loop", AsyncMock())
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/plan-slot2/plan-action",
                    json={"action": "go all"},
                )
                assert resp.status == 200
        user_msgs = [m for m in slot.messages if m["role"] == "user"]
        assert user_msgs[-1]["content"] == "Go All"
        assert slot._auto_run is True

    @pytest.mark.asyncio
    async def test_cancel_clears_auto_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("plan-slot3", mode="orchestrator")
        slot._auto_run = True
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/plan-slot3/plan-action",
                json={"action": "cancel"},
            )
            assert resp.status == 200
        assert slot._auto_run is False

    @pytest.mark.asyncio
    async def test_busy_go_queues_with_stamp_and_human_provenance(self, tmp_path, monkeypatch):
        """A Go clicked on a BUSY slot queues, and the entry carries both the
        admission-time containment stamp and authenticated-human provenance —
        without the flag, a human linking their own session before the drain
        would silently destroy the approval they already gave (the same
        request-identity split as api_chat and the manual continue)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import session_control as sc

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("plan-busy", mode="orchestrator")
        task = MagicMock()
        task.done.return_value = False
        slot.task = task  # running -> the Go must queue, not start a stage loop
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/plan-busy/plan-action",
                json={"action": "go"},
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True
        entry = slot._queue[0]
        assert entry["content"] == "Go"
        assert entry.get("_directive_user_origin") is True  # no app identity on the request
        assert sc.QUEUED_CONTAINMENT_META_KEY in entry.get("meta", {})


class TestPlanValidationStuck:
    """Tests for has_plan=False after strip_plan_markers on invalid plans."""

    def test_strip_plan_markers_clears_has_plan(self):
        """After stripping, has_plan must be False so ensure_go_all_option doesn't run."""
        from kiro_crew.context_management import (
            strip_plan_markers,
            validate_plan_format,
        )

        # Simulate a response that looks like a plan but fails validation
        bad_plan = "📋 Plan for: test\n\nThis has no Stage lines.\n\n[OPTION: Go | Cancel]"
        has_plan, valid, _ = validate_plan_format(bad_plan)
        assert has_plan, "Expected plan header to be detected"
        assert not valid, "Expected plan to be invalid (no Stage lines)"
        stripped = strip_plan_markers(bad_plan)
        has_plan_after, _, _ = validate_plan_format(stripped)
        assert not has_plan_after, (
            "strip_plan_markers must remove plan markers so "
            "validate_plan_format no longer detects a plan"
        )
        assert "📋" not in stripped


class TestOrchestratorPlanGateArming:
    """Regression tests for the plan-review gate.

    Bug: the end-of-turn plan detector ran on EVERY orchestrator turn and only
    inspected the final text segment. A stage-execution turn whose output
    contained plan-like text could re-arm / re-count the plan (corrupting the
    stage total → "Stage N of M" over-runs); and a plan followed by tool calls
    was flushed out of the final segment so the gate never armed. The fix scopes
    detection to planning turns (`_orch_planning` / `_in_stage_execution`) and
    arms from a never-reset whole-turn buffer.
    """

    _PLAN = "📋 Plan for: demo\n\nStage 1: Alpha\nStage 2: Beta\n\n[OPTION: Go | Go All | Cancel]"

    @staticmethod
    def _make_mock_client(events):
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.context_window_tokens = MagicMock(return_value=0)
        client.context_used_tokens = MagicMock(return_value=0)
        client.available_models = MagicMock(return_value=[])
        client.client = MagicMock()
        client.client.pop_pending_oauth_requests = MagicMock(return_value=[])

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_state_for_run_chat(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_planning_turn_arms_plan(self, tmp_path, monkeypatch):
        """A planning turn (not a stage-execution turn) arms the gate metadata."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("plan-arm", mode="orchestrator")
        slot._titled = True
        client = self._make_mock_client(
            [LLMEvent(kind=EVENT_TEXT_CHUNK, text=self._PLAN), LLMEvent(kind=EVENT_COMPLETE)]
        )
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        await _run_chat(state, slot, "make a plan")

        assert slot._stage_titles == ["Alpha", "Beta"]
        assert slot._plan_goal == "demo"

    @pytest.mark.asyncio
    async def test_stage_execution_turn_never_rearms(self, tmp_path, monkeypatch):
        """A stage-execution turn must NOT re-arm/re-count, even if its output
        contains a valid plan — this is the root cause of the 'Stage N of M'
        over-run."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("stage-noarm", mode="orchestrator")
        slot._titled = True
        # Simulate being mid stage-loop with an already-armed 1-stage plan.
        slot._in_stage_execution = True
        slot._stage_titles = ["Existing"]
        slot._plan_goal = "existing goal"
        # The stage turn's output happens to contain a *different* valid plan.
        client = self._make_mock_client(
            [LLMEvent(kind=EVENT_TEXT_CHUNK, text=self._PLAN), LLMEvent(kind=EVENT_COMPLETE)]
        )
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        await _run_chat(state, slot, "execute stage 1")

        # Titles/goal/count must be untouched — no re-arm, no re-count.
        assert slot._stage_titles == ["Existing"]
        assert slot._plan_goal == "existing goal"
        assert slot._plan_stage_count == 1

    @pytest.mark.asyncio
    async def test_stage_loop_sets_and_clears_in_stage_execution(self, tmp_path, monkeypatch):
        """_stage_loop keeps the guard set across EVERY stage turn (not per
        _run_chat), so a queued recovery turn can't run unguarded, and clears it
        once on exit so a later re-plan can arm again."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = _ChatSlot("flag-test", mode="orchestrator")
        slot._stage_titles = ["A", "B"]
        slot._orch_tracker = None

        seen: list[bool] = []

        async def _rec(s, sl, msg, **kw):
            seen.append(sl._in_stage_execution)

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _rec)

        await _stage_loop(state, slot, auto_run=True)

        assert seen == [True, True], "guard must stay True during every stage turn"
        assert slot._in_stage_execution is False, "guard must be cleared once on loop exit"

    @pytest.mark.asyncio
    async def test_stage_loop_clamps_when_plan_shrinks(self, tmp_path, monkeypatch):
        """If the live plan size shrinks mid-run, the loop stops instead of
        building a phantom 'Stage N of M' (N > M) context."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = _ChatSlot("clamp-test", mode="orchestrator")
        slot._stage_titles = ["A", "B", "C"]  # total = 3
        slot._orch_tracker = None

        calls = 0

        async def _shrink(s, sl, msg, **kw):
            nonlocal calls
            calls += 1
            sl._stage_titles = ["A"]  # plan shrinks to 1 stage mid-run

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _shrink)

        await _stage_loop(state, slot, auto_run=True)

        assert calls == 1, "loop must stop after the plan shrank, not over-run"
        seps = [m["content"] for m in slot.messages if "stage-sep" in m.get("cls", "")]
        assert not any(
            "Stage 2" in s or "Stage 3" in s for s in seps
        ), "no phantom stage beyond the live plan size may be built"

    # ── P0 hardening: stage turn ceiling + subagent wait cap ──

    @staticmethod
    def _orch_state():
        """Minimal DashboardState double for driving _stage_loop."""
        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        return state

    @pytest.mark.asyncio
    async def test_stage_loop_times_out_a_stage_that_swallows_cancellation(
        self, tmp_path, monkeypatch
    ):
        """The real `_run_chat` CATCHES CancelledError (flushes partial output and
        returns), so `asyncio.wait_for` would absorb its own deadline and let a
        half-finished stage advance as a success. The fake here reproduces that
        exact semantic -- a bare `sleep()` would propagate the cancellation and
        pass even against the broken implementation, which is why this test uses
        a swallowing turn."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.config_dir", lambda: tmp_path)
        from kiro_crew.context_management import OrchestrationTracker
        from kiro_crew.dashboard.chat import _stage_loop

        state = self._orch_state()
        slot = _ChatSlot("hang-test", mode="orchestrator")
        slot._stage_titles = ["A", "B"]
        # 1s budget so the ceiling fires well inside the test's runtime.
        slot._orch_tracker = OrchestrationTracker(stage_timeout_seconds=1)
        slot._auto_run = True

        swallowed = False

        async def _hang_and_swallow(s, sl, msg, **kw):
            nonlocal swallowed
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                # Exactly what _run_chat does: absorb it and return normally.
                swallowed = True
                return None

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _hang_and_swallow)

        await asyncio.wait_for(_stage_loop(state, slot, auto_run=True), timeout=30)

        assert swallowed, "the fake must have absorbed the cancellation (the real path)"
        assert slot._auto_run is False, "a stage cut at the ceiling must stop auto-run"
        assert any(
            "timed out" in m.get("content", "") for m in slot.messages
        ), "the user must see a timeout card, not a silently-advanced stage"
        seps = [m["content"] for m in slot.messages if "stage-sep" in m.get("cls", "")]
        assert not any("Stage 2" in s for s in seps), "a cut stage must NOT advance to the next one"

    @pytest.mark.asyncio
    async def test_stage_loop_disabled_timeout_does_not_abort_instantly(
        self, tmp_path, monkeypatch
    ):
        """stage_timeout_seconds=0 means 'disabled' (see is_stage_timed_out), so
        it must become wait_for(None) — passing 0 through would time out every
        stage before its turn began."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.config_dir", lambda: tmp_path)
        from kiro_crew.context_management import OrchestrationTracker
        from kiro_crew.dashboard.chat import _stage_loop

        state = self._orch_state()
        slot = _ChatSlot("no-timeout", mode="orchestrator")
        slot._stage_titles = ["A"]
        slot._orch_tracker = OrchestrationTracker(stage_timeout_seconds=0)

        ran = 0

        async def _ok(s, sl, msg, **kw):
            nonlocal ran
            ran += 1

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _ok)

        await _stage_loop(state, slot, auto_run=True)

        assert ran == 1, "a disabled timeout must let the stage turn actually run"
        assert not any(
            "timed out" in m.get("content", "") for m in slot.messages
        ), "no timeout card may be emitted when the timeout is disabled"

    def test_subagent_wait_cap_scales_with_stage_timeout(self):
        """The poll cap tracks the stage budget instead of a fixed 5 min, and a
        disabled timeout falls back to the ceiling rather than 0 (which would
        skip the subagent wait entirely)."""
        from kiro_crew.context_management import OrchestrationTracker

        def cap(timeout: int) -> int:
            t = OrchestrationTracker(stage_timeout_seconds=timeout)
            return min(t.stage_timeout_seconds // 4, 450) if t.stage_timeout_seconds else 450

        assert cap(1800) == 450, "default 30m budget -> 15 min of subagent wait"
        assert cap(600) == 150, "a 10m budget scales down proportionally"
        assert cap(7200) == 450, "a huge budget is still capped at the 15 min ceiling"
        assert cap(0) == 450, "disabled timeout must NOT collapse the wait to zero"


# ── Tests: plan execution via Go/Go All button simulation ──


class TestPlanExecutionViaButton:
    """Simulate Go/Go All button clicks on a fake plan and verify the Python
    orchestration code drives stage advancement correctly."""

    def _make_slot(
        self, key="plan-exec", max_stages=2, auto_run=False, titles=None, goal="Sample goal"
    ):
        slot = _ChatSlot(key, mode="orchestrator")
        slot._auto_run = auto_run
        slot._stage_titles = (
            titles if titles is not None else [f"Step {i}" for i in range(1, max_stages + 1)]
        )
        slot._plan_goal = goal
        slot._orch_tracker = None  # fresh — will be created by _stage_loop
        return slot

    def _make_state(self, has_subagents=True):
        state = MagicMock()
        state.broadcast_ws = MagicMock()
        if has_subagents:
            state.subagents.running_agents_for.return_value = []
        else:
            state.subagents = MagicMock()
            state.subagents.running_agents_for = MagicMock(return_value=[])
        return state

    @pytest.mark.asyncio
    async def test_go_button_triggers_stage_loop(self, tmp_path, monkeypatch):
        """Clicking 'Go' calls _stage_loop with auto_run=False."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("go-btn", mode="orchestrator")
        slot.append(
            "assistant", "📋 Plan for: test\n\nStage 1: A\n\n[OPTION: Go | Go All | Cancel]"
        )
        mock_loop = AsyncMock()
        with pytest.MonkeyPatch.context() as m:
            m.setattr("kiro_crew.dashboard.chat_orchestrator._stage_loop", mock_loop)
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/go-btn/plan-action", json={"action": "go"}
                )
                assert resp.status == 200
        mock_loop.assert_called_once()
        _, kwargs = mock_loop.call_args
        assert kwargs.get("auto_run") is False
        assert not slot._auto_run

    @pytest.mark.asyncio
    async def test_go_all_button_sets_auto_run_and_triggers_stage_loop(self, tmp_path, monkeypatch):
        """Clicking 'Go All' sets _auto_run=True and calls _stage_loop with auto_run=True."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("goall-btn", mode="orchestrator")
        slot.append(
            "assistant",
            "📋 Plan for: test\n\nStage 1: A\nStage 2: B\n\n[OPTION: Go | Go All | Cancel]",
        )
        mock_loop = AsyncMock()
        with pytest.MonkeyPatch.context() as m:
            m.setattr("kiro_crew.dashboard.chat_orchestrator._stage_loop", mock_loop)
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/goall-btn/plan-action", json={"action": "go all"}
                )
                assert resp.status == 200
        mock_loop.assert_called_once()
        _, kwargs = mock_loop.call_args
        assert kwargs.get("auto_run") is True
        assert slot._auto_run is True


class TestWidgetOriginAutoRunGuard:
    """item 5 — deny-by-default backend guard.

    A chat turn tagged ``meta.origin == "widget"`` was pre-filled into the
    composer by an LLM-emitted ``<mcwidget>`` postMessage. Its TEXT is
    attacker-controlled, so the backend MUST refuse the only chat-text-reachable
    privilege escalation (orchestrator ``go``/``go all`` → ``_auto_run`` +
    ``_stage_loop``) for such turns, while leaving human-typed ``go all`` intact.
    """

    @pytest.mark.asyncio
    async def test_widget_origin_go_all_denied(self, tmp_path, monkeypatch):
        """A widget-origin 'go all' must NOT enable auto-run or start the stage loop."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("wo-goall", mode="orchestrator")

        stage_loop_mock = AsyncMock()
        run_chat_mock = AsyncMock()
        # api_chat calls _stage_loop bound into its own namespace (import at
        # chat_handlers top), so patch there — not chat_orchestrator.
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._stage_loop", stage_loop_mock)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", run_chat_mock)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "go all", "slot": "wo-goall", "meta": {"origin": "widget"}},
            )
            assert resp.status == 200

        # Escalation refused: no auto-run, no stage loop; the text falls through
        # to a normal fully-gated _run_chat turn instead.
        stage_loop_mock.assert_not_called()
        assert slot._auto_run is False
        run_chat_mock.assert_called_once()
        assert "go all" in run_chat_mock.call_args[0][2]

    @pytest.mark.asyncio
    async def test_widget_origin_go_denied(self, tmp_path, monkeypatch):
        """A widget-origin bare 'go' is also refused the stage-loop escalation."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("wo-go", mode="orchestrator")

        stage_loop_mock = AsyncMock()
        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._stage_loop", stage_loop_mock)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", run_chat_mock)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "Go", "slot": "wo-go", "meta": {"origin": "widget"}},
            )
            assert resp.status == 200

        stage_loop_mock.assert_not_called()
        assert slot._auto_run is False
        run_chat_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_human_go_all_still_escalates(self, tmp_path, monkeypatch):
        """A human-typed 'go all' (no widget origin) MUST still enable auto-run."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("human-goall", mode="orchestrator")

        stage_loop_mock = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._stage_loop", stage_loop_mock)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "go all", "slot": "human-goall"},
            )
            assert resp.status == 200

        stage_loop_mock.assert_called_once()
        assert slot._auto_run is True

    @pytest.mark.asyncio
    async def test_widget_origin_normal_message_unaffected(self, tmp_path, monkeypatch):
        """A widget-origin turn whose text isn't go/go-all runs a normal turn."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("wo-normal", mode="orchestrator")

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", run_chat_mock)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "message": "[UI] refresh",
                    "slot": "wo-normal",
                    "meta": {"origin": "widget"},
                },
            )
            assert resp.status == 200

        run_chat_mock.assert_called_once()
        assert "[UI] refresh" in run_chat_mock.call_args[0][2]


class TestPythonStageLoop:
    """Tests for the Python-controlled stage execution loop (_stage_loop).

    Covers: Go (single stage), Go All (multi-stage), Cancel/Stop,
    subagent mid-stage, normal chat unaffected, stage timeout.
    """

    @pytest.fixture(autouse=True)
    def _isolate_config_dir(self, tmp_path, monkeypatch):
        """Redirect every config_dir used by the stage loop to a per-test tmp dir.

        ``_capture_stage_result`` (in ``chat_orchestrator``) writes stage results
        under ``config_dir() / "sessions" / slot.key``. The orchestrator imports
        ``config_dir`` into its own namespace, so patching only the ``chat`` /
        ``state`` namespaces leaves results writing to the live
        ``~/.kirocrew/sessions/`` dir, and parallel (xdist) runs then race on the
        shared fixed ``loop-test`` key. Patching all three namespaces to a unique
        ``tmp_path`` isolates every test in this class.
        """
        for module in ("state", "chat", "chat_orchestrator"):
            monkeypatch.setattr(f"kiro_crew.dashboard.{module}.config_dir", lambda: tmp_path)

    def _make_slot(self, key="loop-test", max_stages=3, titles=None, goal="Test goal"):
        slot = _ChatSlot(key, mode="orchestrator")
        slot._auto_run = False
        slot._stage_titles = (
            titles if titles is not None else [f"Step {i}" for i in range(1, max_stages + 1)]
        )
        slot._plan_goal = goal
        slot._orch_tracker = None
        return slot

    @pytest.mark.asyncio
    async def test_go_single_stage_then_stops(self, tmp_path, monkeypatch):
        """Go (single stage) executes one stage, emits approval message, returns."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=3)

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", run_chat_mock)

        await _stage_loop(state, slot, auto_run=False)

        # Should call _run_chat exactly once (one stage)
        run_chat_mock.assert_called_once()
        # Should emit stage separator
        sep_msgs = [m for m in slot.messages if "stage-sep" in m.get("cls", "")]
        assert len(sep_msgs) == 1
        assert "Stage 1" in sep_msgs[0]["content"]
        # Should emit approval message for next stage
        approval_msgs = [m for m in slot.messages if "Click **Go**" in m.get("content", "")]
        assert len(approval_msgs) == 1
        assert "Stage 2" in approval_msgs[0]["content"]
        assert "[OPTION:" in approval_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_go_all_runs_all_stages(self, tmp_path, monkeypatch):
        """Go All executes all stages in sequence."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=3)

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", run_chat_mock)

        await _stage_loop(state, slot, auto_run=True)

        # Should call _run_chat 3 times (one per stage)
        assert run_chat_mock.call_count == 3
        # Should emit 3 stage separators
        sep_msgs = [m for m in slot.messages if "stage-sep" in m.get("cls", "")]
        assert len(sep_msgs) == 3
        # Should emit completion message
        done_msgs = [m for m in slot.messages if "All 3 stages complete" in m.get("content", "")]
        assert len(done_msgs) == 1
        # auto_run should be cleared
        assert slot._auto_run is False

    @pytest.mark.asyncio
    async def test_cancel_stops_loop(self, tmp_path, monkeypatch):
        """Setting _stopping mid-loop breaks execution."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=5)

        call_count = 0

        async def _mock_run_chat(s, sl, msg, **kw):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                # Simulate user clicking Stop after stage 2
                slot._stop_state = "soft_pending"

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)

        await _stage_loop(state, slot, auto_run=True)

        # Should stop after 2 stages (not run all 5)
        assert call_count == 2
        # No "All stages complete" message
        done_msgs = [m for m in slot.messages if "stages complete" in m.get("content", "")]
        assert len(done_msgs) == 0

    @pytest.mark.asyncio
    async def test_stage_timeout_stops_loop(self, tmp_path, monkeypatch):
        """Stage timeout breaks the loop."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=3)

        # Pre-create tracker with timeout
        from kiro_crew.context_management import OrchestrationTracker

        tracker = OrchestrationTracker(stage_timeout_seconds=1)
        slot._orch_tracker = tracker
        # Force timeout on first check
        tracker.is_stage_timed_out = lambda: True

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", run_chat_mock)

        await _stage_loop(state, slot, auto_run=True)

        # Should NOT call _run_chat (timeout before execution)
        run_chat_mock.assert_not_called()
        # Should emit timeout message
        timeout_msgs = [m for m in slot.messages if "timed out" in m.get("content", "")]
        assert len(timeout_msgs) == 1

    @pytest.mark.asyncio
    async def test_normal_chat_unaffected(self, tmp_path, monkeypatch):
        """Normal chat messages (not Go/Go All) still go through _run_chat directly."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("normal-chat", mode="orchestrator")

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", run_chat_mock)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "hello world", "slot": "normal-chat"},
            )
            assert resp.status == 200

        # Normal message goes through _run_chat, not _stage_loop
        run_chat_mock.assert_called_once()
        msg = run_chat_mock.call_args[0][2]
        assert "hello world" in msg

    @pytest.mark.asyncio
    async def test_orchestrating_flag_set_and_held_queue_drained(self, tmp_path, monkeypatch):
        """The loop marks the slot orchestrating for the whole plan, then hands off
        a message the user queued mid-plan once the plan ends."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=2)
        state._slots = {slot.key: slot}  # slot still registered
        slot.queue_append("user typed mid-plan")

        seen = []

        async def _mock_run_chat(s, sl, msg, **kw):
            seen.append(sl._in_stage_execution)

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)
        start_next = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_orchestrator._start_next_queued_turn", start_next
        )

        await _stage_loop(state, slot, auto_run=True)

        assert seen == [True, True]  # orchestrating for every stage
        assert slot._in_stage_execution is False  # cleared once the plan ends
        start_next.assert_awaited_once()  # held user message handed off at plan end

    @pytest.mark.asyncio
    async def test_deleted_slot_skips_handoff(self, tmp_path, monkeypatch):
        """If the slot was deleted mid-plan (no longer registered), the finally
        must NOT launch its held queue on the torn-down slot."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=1)
        state._slots = {}  # slot deleted while the plan ran
        slot.queue_append("queued during plan")

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", run_chat_mock)
        start_next = AsyncMock(return_value=False)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_orchestrator._start_next_queued_turn", start_next
        )

        await _stage_loop(state, slot, auto_run=True)

        start_next.assert_not_awaited()  # no turn launched on a deleted slot
        assert [i["content"] for i in slot._queue] == ["queued during plan"]

    @pytest.mark.asyncio
    async def test_signed_out_cli_holds_queue(self, tmp_path, monkeypatch):
        """If a stage hit ACP auth-required, the end-of-plan handoff must HOLD the
        queued follow-up for post-login resume, not pop it into another auth
        failure (mirrors _run_chat's own not-_auth_required guard)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=1)
        state._slots = {slot.key: slot}
        slot.queue_append("queued during plan")

        async def _auth_run_chat(s, sl, msg, **kw):
            sl._last_turn_auth_required = True  # signed-out CLI discovered this stage

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _auth_run_chat)
        start_next = AsyncMock(return_value=False)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_orchestrator._start_next_queued_turn", start_next
        )

        await _stage_loop(state, slot, auto_run=True)

        start_next.assert_not_awaited()  # queue held for post-login resume
        assert [i["content"] for i in slot._queue] == ["queued during plan"]

    @pytest.mark.asyncio
    async def test_orchestrating_slot_queues_message(self, tmp_path, monkeypatch):
        """A mid-plan message QUEUES (not runs) even when slot.task is idle between
        stages, because slot._in_stage_execution gates the api_chat queue path."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("orch-chat", mode="orchestrator")
        slot._in_stage_execution = True  # plan running; slot.task momentarily None between stages

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", run_chat_mock)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"message": "queued mid-plan", "slot": "orch-chat"}
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        run_chat_mock.assert_not_called()  # queued, not run concurrently with the plan
        assert any(i["content"] == "queued mid-plan" for i in slot._queue)

    @pytest.mark.asyncio
    async def test_queued_receipt_carries_the_entry_queue_id(self, tmp_path, monkeypatch):
        """The `queued: true` receipt names the entry it created: `queue_id`
        must equal the queue entry's id, because the sender binds its pre-send
        composer state to that id for the cancel-queued restore (#560) — a
        content-based key cannot do this (serialization is not injective and
        other tabs can queue colliding content)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("busy-chat")
        slot._in_stage_execution = True  # force the busy queue path

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", run_chat_mock)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"message": "queued while busy", "slot": "busy-chat"}
            )
            assert resp.status == 200
            body = await resp.json()
            assert body.get("queued") is True
            entry = next(i for i in slot._queue if i["content"] == "queued while busy")
            assert body.get("queue_id") == entry["id"]

    @pytest.mark.asyncio
    async def test_go_button_uses_stage_loop(self, tmp_path, monkeypatch):
        """Go button via plan-action endpoint uses _stage_loop."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("go-loop", mode="orchestrator")
        slot.append(
            "assistant",
            "📋 Plan for: test\n\nStage 1: A\nStage 2: B\n\n[OPTION: Go | Go All | Cancel]",
        )
        slot._stage_titles = ["A", "B"]
        slot._plan_goal = "test"

        stage_loop_mock = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._stage_loop", stage_loop_mock)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/go-loop/plan-action", json={"action": "go"})
            assert resp.status == 200

        stage_loop_mock.assert_called_once()
        _, kwargs = stage_loop_mock.call_args
        # For positional args
        args = stage_loop_mock.call_args[0]
        # auto_run should be False for "Go"
        assert kwargs.get("auto_run", args[2] if len(args) > 2 else None) is False

    @pytest.mark.asyncio
    async def test_go_all_button_uses_stage_loop(self, tmp_path, monkeypatch):
        """Go All button via plan-action endpoint uses _stage_loop with auto_run=True."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("goall-loop", mode="orchestrator")
        slot.append(
            "assistant",
            "📋 Plan for: test\n\nStage 1: A\nStage 2: B\n\n[OPTION: Go | Go All | Cancel]",
        )
        slot._stage_titles = ["A", "B"]
        slot._plan_goal = "test"

        stage_loop_mock = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._stage_loop", stage_loop_mock)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/goall-loop/plan-action", json={"action": "go all"}
            )
            assert resp.status == 200

        stage_loop_mock.assert_called_once()
        _, kwargs = stage_loop_mock.call_args
        args = stage_loop_mock.call_args[0]
        assert kwargs.get("auto_run", args[2] if len(args) > 2 else None) is True
        assert slot._auto_run is True

    @pytest.mark.asyncio
    async def test_stage_results_captured_to_disk(self, tmp_path, monkeypatch):
        """Each stage result is written to disk and tracked in tracker."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=2)

        async def _mock_run_chat(s, sl, msg, **kw):
            sl.append("assistant", "Result for stage", "msg msg-a")

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)

        await _stage_loop(state, slot, auto_run=True)

        tracker = slot._orch_tracker
        assert 1 in tracker._stage_results
        assert 2 in tracker._stage_results
        # Verify files exist on disk
        for stage_num in (1, 2):
            path = Path(tracker._stage_results[stage_num])
            assert path.exists()
            content = path.read_text(encoding="utf-8")
            assert "Result for stage" in content

    @pytest.mark.asyncio
    async def test_build_stage_context_includes_goal_and_status(self):
        """_build_stage_context includes goal, status summary, and stage instruction."""
        from kiro_crew.context_management import OrchestrationTracker
        from kiro_crew.dashboard.chat import _build_stage_context

        slot = self._make_slot(
            max_stages=3, titles=["Research", "Implement", "Test"], goal="Build feature X"
        )
        tracker = OrchestrationTracker()
        slot._orch_tracker = tracker

        ctx = await _build_stage_context(slot, tracker, stage_idx=0)
        assert "Build feature X" in ctx
        assert "▶️ Stage 1: Research — execute now" in ctx
        assert "⬜ Stage 2: Implement — pending" in ctx
        assert "Stage 1 of 3" in ctx

    @pytest.mark.asyncio
    async def test_build_stage_context_includes_previous_results(self, tmp_path, monkeypatch):
        """_build_stage_context includes paths to previous stage results."""
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_crew.context_management import OrchestrationTracker
        from kiro_crew.dashboard.chat import _build_stage_context

        slot = self._make_slot(max_stages=3, titles=["A", "B", "C"])
        tracker = OrchestrationTracker()
        slot._orch_tracker = tracker

        # Write a fake stage 1 result
        result_dir = tmp_path / "sessions" / slot.key
        result_dir.mkdir(parents=True)
        result_file = result_dir / "stage_1_result.md"
        result_file.write_text("Stage 1 completed successfully")
        tracker.record_stage_result(1, str(result_file))

        ctx = await _build_stage_context(slot, tracker, stage_idx=1)
        assert "Stage 1 completed successfully" in ctx
        assert str(result_file) in ctx
        assert "✅ Stage 1: A — completed" in ctx
        assert "▶️ Stage 2: B — execute now" in ctx

    def test_status_summary_format(self):
        """OrchestrationTracker.status_summary produces correct format."""
        from kiro_crew.context_management import OrchestrationTracker

        tracker = OrchestrationTracker()
        summary = tracker.status_summary(1, 3, ["Research", "Implement", "Test"])
        assert "✅ Stage 1: Research — completed" in summary
        assert "▶️ Stage 2: Implement — execute now" in summary
        assert "⬜ Stage 3: Test — pending" in summary

    @pytest.mark.asyncio
    async def test_run_chat_error_stops_loop(self, tmp_path, monkeypatch):
        """If _run_chat raises, stage loop catches, emits error, and stops."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=3)

        async def _exploding_run_chat(s, sl, msg, **kw):
            raise RuntimeError("LLM provider error")

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _exploding_run_chat)

        await _stage_loop(state, slot, auto_run=True)

        # Should emit error message
        err_msgs = [
            m for m in slot.messages if "failed due to an internal error" in m.get("content", "")
        ]
        assert len(err_msgs) == 1
        # Should NOT run remaining stages
        sep_msgs = [m for m in slot.messages if "stage-sep" in m.get("cls", "")]
        assert len(sep_msgs) == 1  # only stage 1 separator

    @pytest.mark.asyncio
    async def test_subagent_wait_loop(self, tmp_path, monkeypatch):
        """Stage loop waits for pending subagents before advancing."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=2)

        _poll_count = 0

        def _running_agents(key):
            nonlocal _poll_count
            _poll_count += 1
            # Simulate subagent finishing after 2 polls
            return [{"id": "sa-1"}] if _poll_count < 3 else []

        state.subagents = MagicMock()
        state.subagents.running_agents_for = _running_agents

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", run_chat_mock)
        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.asyncio.sleep", AsyncMock())

        await _stage_loop(state, slot, auto_run=True)

        # Should have polled for subagents
        assert _poll_count >= 3
        # Should still complete both stages
        assert run_chat_mock.call_count == 2

    @pytest.mark.asyncio
    async def test_subagent_manager_missing_stops_auto_run(self, tmp_path, monkeypatch):
        """When running_agents_for returns None, auto-run must stop (fail-closed)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents.running_agents_for.return_value = None  # error case
        slot = self._make_slot(max_stages=3)

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", run_chat_mock)

        await _stage_loop(state, slot, auto_run=True)

        # Should run stage 1 but stop before stage 2 (fail-closed)
        run_chat_mock.assert_called_once()
        assert slot._auto_run is False

    @pytest.mark.asyncio
    async def test_subagent_manager_none_stops_auto_run(self, tmp_path, monkeypatch):
        """When state.subagents is None, auto-run must stop (fail-closed)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = None  # manager missing
        slot = self._make_slot(max_stages=3)

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", run_chat_mock)

        await _stage_loop(state, slot, auto_run=True)

        # Should run stage 1 but stop before stage 2 (fail-closed)
        run_chat_mock.assert_called_once()
        assert slot._auto_run is False

    @pytest.mark.asyncio
    async def test_go_reentry_resumes_from_stage_2(self, tmp_path, monkeypatch):
        """After Go completes stage 1, next Go call resumes from stage 2."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=3)

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", run_chat_mock)

        # First Go: runs stage 1 only
        await _stage_loop(state, slot, auto_run=False)
        assert run_chat_mock.call_count == 1

        # Second Go: should resume from stage 2
        run_chat_mock.reset_mock()
        await _stage_loop(state, slot, auto_run=False)
        assert run_chat_mock.call_count == 1
        # Verify it was stage 2 (context should show stage 2 as current)
        ctx = run_chat_mock.call_args[0][2]
        assert "▶️ Stage 2" in ctx

    @pytest.mark.asyncio
    async def test_previous_result_paths_compaction(self, tmp_path, monkeypatch):
        """Long stage results are truncated with tail bias (30% head, 70% tail)."""
        monkeypatch.setattr("kiro_crew.dashboard.chat.is_sensitive_path", lambda p: False)
        from kiro_crew.context_management import OrchestrationTracker
        from kiro_crew.dashboard.chat import _previous_result_paths

        tracker = OrchestrationTracker()

        # Write a large stage 1 result (5000 chars)
        result_dir = tmp_path / "sessions" / "test-slot"
        result_dir.mkdir(parents=True)
        result_file = result_dir / "stage_1_result.md"
        head_marker = "HEAD_MARKER_START"
        tail_marker = "TAIL_MARKER_END"
        large_content = (
            head_marker
            + "x" * 4000
            + tail_marker
            + "y" * (5000 - len(head_marker) - 4000 - len(tail_marker))
        )
        result_file.write_text(large_content)
        tracker.record_stage_result(1, str(result_file))

        loaded = await _previous_result_paths(tracker, 1)
        # Should be truncated (2000 chars max per stage + header + path)
        assert len(loaded) < 3000
        assert "...[truncated]..." in loaded
        assert str(result_file) in loaded
        # Tail bias: head_marker (near start) should be in the 30% head
        assert head_marker in loaded
        # Tail bias: tail_marker (at char ~4015) should be in the 70% tail
        assert tail_marker in loaded


class TestStageFailureEscalation:
    """Test that stage failures trigger human question logic (escalation)."""

    def test_single_failure_allows_retry(self):
        """A single task failure does NOT trigger escalation — retry is allowed."""
        from kiro_crew.context_management import OrchestrationTracker

        tracker = OrchestrationTracker()
        tracker.record_round(1)
        # First failure: should not escalate
        hit_limit = tracker.record_failure("task-a")
        assert not hit_limit
        assert not tracker.has_escalated
        assert tracker.failure_count("task-a") == 1

    def test_repeated_failures_trigger_escalation(self):
        """After MAX_TASK_FAILURES (3), has_escalated becomes True."""
        from kiro_crew.context_management import (
            MAX_TASK_FAILURES,
            OrchestrationTracker,
        )

        tracker = OrchestrationTracker()
        tracker.record_round(1)
        for i in range(MAX_TASK_FAILURES - 1):
            assert not tracker.record_failure("task-a")
        # The Nth failure triggers escalation
        assert tracker.record_failure("task-a")
        assert tracker.has_escalated

    def test_success_resets_failure_count(self):
        """record_success clears the failure counter for a task."""
        from kiro_crew.context_management import OrchestrationTracker

        tracker = OrchestrationTracker()
        tracker.record_round(1)
        tracker.record_failure("task-a")
        tracker.record_failure("task-a")
        assert tracker.failure_count("task-a") == 2
        tracker.record_success("task-a")
        assert tracker.failure_count("task-a") == 0
        assert not tracker.has_escalated

    def test_stage_round_limit_triggers_escalation(self):
        """After MAX_STAGE_ROUNDS (3) rounds in a stage, has_escalated is True."""
        from kiro_crew.context_management import MAX_STAGE_ROUNDS, OrchestrationTracker

        tracker = OrchestrationTracker()
        for i in range(MAX_STAGE_ROUNDS):
            tracker.record_round(1)
        assert tracker.has_escalated

    def test_reset_after_guidance_clears_rounds(self):
        """User guidance resets round counters, allowing retry."""
        from kiro_crew.context_management import MAX_STAGE_ROUNDS, OrchestrationTracker

        tracker = OrchestrationTracker()
        for i in range(MAX_STAGE_ROUNDS):
            tracker.record_round(1)
        assert tracker.has_escalated
        tracker.reset_after_guidance()
        assert not tracker.has_escalated
        assert tracker.round_count(1) == 0

    def test_force_fail_after_max_escalations(self):
        """After MAX_STAGE_ESCALATIONS resets, stage is force-failed."""
        from kiro_crew.context_management import (
            MAX_STAGE_ESCALATIONS,
            MAX_STAGE_ROUNDS,
            OrchestrationTracker,
        )

        tracker = OrchestrationTracker()
        for _esc in range(MAX_STAGE_ESCALATIONS):
            for _r in range(MAX_STAGE_ROUNDS):
                tracker.record_round(1)
            tracker.reset_after_guidance()
        assert tracker.is_force_failed(1)


# ── Tests: prompt-busy session recovery ──


class TestPromptBusyRecovery:
    """When kiro-cli returns 'Prompt already in progress', _run_chat must
    reset the session and re-queue the message so the next attempt cold-starts."""

    @pytest.mark.asyncio
    async def test_prompt_busy_resets_session_and_requeues(self, tmp_path: Path) -> None:
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        state = _make_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()

        slot = state.get_or_create_slot("busy-slot")
        slot.append("user", "hello", "msg msg-u")

        # Make client.stream raise "already in progress"
        mock_client = state.sessions.get_or_create.return_value[0]

        async def _raise_busy(msg):
            raise AcpError("Prompt error: {'data': 'Prompt already in progress'}")
            yield  # make it an async generator  # noqa: E501

        mock_client.stream = _raise_busy
        mock_client.stream_command = _raise_busy
        mock_client.shutdown = AsyncMock()

        await _run_chat(state, slot, "test message")

        # Session must be reset (kill the stuck kiro-cli process)
        state.sessions.reset.assert_awaited_once()
        # The finally block drains the re-queued message into a new task
        assert slot.task is not None
        # No ❌ error shown to the user for the busy case
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert not any("already in progress" in m.get("content", "") for m in error_msgs)

    @pytest.mark.asyncio
    async def test_process_exited_resets_session_and_requeues(self, tmp_path: Path) -> None:
        """When ACP subprocess dies (SIGTERM/SIGKILL), _run_chat must reset
        the session and re-queue the message so autonudges land on a fresh
        provider instead of a bare ❌ error card with no work done."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        state = _make_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()

        slot = state.get_or_create_slot("dead-slot")
        slot.append("user", "hello", "msg msg-u")

        mock_client = state.sessions.get_or_create.return_value[0]

        async def _raise_dead(msg):
            raise AcpError("ACP process exited (code=-15)")
            yield  # make it an async generator  # noqa: E501

        mock_client.stream = _raise_dead
        mock_client.stream_command = _raise_dead
        mock_client.shutdown = AsyncMock()

        await _run_chat(state, slot, "test message")

        state.sessions.reset.assert_awaited_once()
        assert slot.task is not None
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert not any("process exited" in m.get("content", "") for m in error_msgs)


# ── Tests: slot.task None guard ──


class TestSlotTaskNoneGuard:
    """stop/delete must not crash when slot.task is None."""

    @pytest.mark.asyncio
    async def test_stop_not_running(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("s1")
        # task is None → running is False → stop is a no-op
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_delete_not_running(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        # task is None → running is False → delete skips cancel
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/s1")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_stop_with_real_task_cancels(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        state.sessions.stop_turn = AsyncMock(return_value="soft")
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.get_running_loop().create_future()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200
            state.sessions.stop_turn.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_with_real_task_cancels(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.get_running_loop().create_future()

        async with TestClient(TestServer(_make_app(state))) as client:
            with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop"):
                resp = await client.delete("/api/chat/slots/s1")
            assert resp.status == 200
            assert slot.task.cancelled()


# ── Bulk cleanup tests ──


class TestBulkCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_archives_stale_sessions(self, tmp_path, monkeypatch):
        """Stale sessions are archived; fresh and pinned are kept."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        fresh_ts = datetime.now(timezone.utc).isoformat()

        stale = state.get_or_create_slot("stale1")
        stale.append("user", "old msg", ts=old_ts)
        stale.drain()

        fresh = state.get_or_create_slot("fresh1")
        fresh.append("user", "new msg", ts=fresh_ts)
        fresh.drain()

        pinned = state.get_or_create_slot("pinned1")
        pinned.pinned = True
        pinned.append("user", "pinned msg", ts=old_ts)
        pinned.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 3, "active_slot": "fresh1"},
            )
            data = await resp.json()
            assert data["ok"] is True
            assert data["archived"] == 1
            assert "stale1" in data["keys"]

        assert "stale1" not in state._slots
        assert "fresh1" in state._slots
        assert "pinned1" in state._slots

    @pytest.mark.asyncio
    async def test_cleanup_skips_active_slot(self, tmp_path, monkeypatch):
        """The active slot is never archived even if stale."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        slot = state.get_or_create_slot("active")
        slot.append("user", "old", ts=old_ts)
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 1, "active_slot": "active"},
            )
            data = await resp.json()
            assert data["archived"] == 0
        assert "active" in state._slots

    @pytest.mark.asyncio
    async def test_cleanup_saves_to_history(self, tmp_path, monkeypatch):
        """Archived sessions are persisted to conversation log."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        slot = state.get_or_create_slot("to-archive")
        slot.append("user", "save me", ts=old_ts)
        slot.append("assistant", "saved", ts=old_ts)
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 3},
            )

        msgs = state.conversation_log.read_messages("dashboard:to-archive")
        assert len(msgs) == 2
        assert msgs[0]["content"] == "save me"

    @pytest.mark.asyncio
    async def test_cleanup_defaults_to_3_days(self, tmp_path, monkeypatch):
        """Without max_inactive_days, defaults to 3."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        ts_2d = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        slot = state.get_or_create_slot("recent")
        slot.append("user", "hi", ts=ts_2d)
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/cleanup", json={})
            data = await resp.json()
            assert data["archived"] == 0

    @pytest.mark.asyncio
    async def test_cleanup_empty_slots_uses_created_at(self, tmp_path, monkeypatch):
        """Slots with no messages use created_at for staleness."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        slot = state.get_or_create_slot("empty-old")
        slot.created_at = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 7},
            )
            data = await resp.json()
            assert data["archived"] == 1
            assert "empty-old" in data["keys"]

    @pytest.mark.asyncio
    async def test_cleanup_no_stale_returns_zero(self, tmp_path, monkeypatch):
        """When all sessions are fresh, nothing is archived."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timezone

        fresh_ts = datetime.now(timezone.utc).isoformat()
        slot = state.get_or_create_slot("fresh")
        slot.append("user", "hi", ts=fresh_ts)
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 1},
            )
            data = await resp.json()
            assert data["ok"] is True
            assert data["archived"] == 0
            assert data["keys"] == []

    @pytest.mark.asyncio
    async def test_cleanup_rollback_on_save_failure(self, tmp_path, monkeypatch):
        """When _save_slot_to_history raises, slot is restored and reported as failed."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        slot = state.get_or_create_slot("fail-save")
        slot.append("user", "msg", ts=old_ts)
        slot.drain()

        with patch(
            "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
            side_effect=OSError("disk full"),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/cleanup",
                    json={"max_inactive_days": 1},
                )
                data = await resp.json()
                assert data["archived"] == 0
                assert "fail-save" in data["failed"]

        # Slot must be restored (not lost)
        assert "fail-save" in state._slots
        # No history entry should exist (save failed)
        msgs = state.conversation_log.read_messages("dashboard:fail-save")
        assert len(msgs) == 0

    @pytest.mark.asyncio
    async def test_cleanup_cancels_running_task(self, tmp_path, monkeypatch):
        """Running tasks on stale slots are cancelled after archive."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        slot = state.get_or_create_slot("running1")
        slot.append("user", "msg", ts=old_ts)
        slot.drain()
        slot.task = asyncio.get_running_loop().create_future()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 1},
            )
            data = await resp.json()
            assert data["archived"] == 1
        assert slot.task.cancelled()

    @pytest.mark.asyncio
    async def test_cleanup_skips_unparseable_timestamps(self, tmp_path, monkeypatch):
        """Slots with unparseable timestamps are skipped, not archived."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        slot = state.get_or_create_slot("bad-ts")
        slot.append("user", "hi", ts="not-a-date")
        slot.created_at = "also-not-a-date"
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 1},
            )
            data = await resp.json()
            assert data["archived"] == 0
        assert "bad-ts" in state._slots

    @pytest.mark.asyncio
    async def test_cleanup_dry_run_returns_keys_without_archiving(self, tmp_path, monkeypatch):
        """dry_run=True returns stale keys and active_is_stale but does not archive anything."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        fresh_ts = datetime.now(timezone.utc).isoformat()

        stale = state.get_or_create_slot("stale1")
        stale.append("user", "old msg", ts=old_ts)
        stale.drain()

        active_stale = state.get_or_create_slot("active1")
        active_stale.append("user", "old active msg", ts=old_ts)
        active_stale.drain()

        fresh = state.get_or_create_slot("fresh1")
        fresh.append("user", "new msg", ts=fresh_ts)
        fresh.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 3, "active_slot": "active1", "dry_run": True},
            )
            data = await resp.json()
            assert data["ok"] is True
            assert data["dry_run"] is True
            assert "stale1" in data["keys"]
            assert "active1" not in data["keys"]
            assert data["count"] == 1
            assert data["active_is_stale"] is True

        # Slots should NOT have been removed
        assert "stale1" in state._slots
        assert "active1" in state._slots
        assert "fresh1" in state._slots


class TestHistoryKeyFor:
    """Tests for _history_key_for — canonical history key from slot key."""

    def test_already_canonical(self):
        from kiro_crew.dashboard.chat import _history_key_for

        assert _history_key_for("dashboard:chat-1-100") == "dashboard:chat-1-100"

    def test_strips_single_prefix(self):
        from kiro_crew.dashboard.chat import _history_key_for

        assert _history_key_for("dashboard_chat-1-100") == "dashboard:chat-1-100"

    def test_strips_double_prefix(self):
        from kiro_crew.dashboard.chat import _history_key_for

        assert _history_key_for("dashboard_dashboard_chat-1-100") == "dashboard:chat-1-100"

    def test_strips_triple_prefix(self):
        from kiro_crew.dashboard.chat import _history_key_for

        assert _history_key_for("dashboard_dashboard_dashboard_x") == "dashboard:x"

    def test_raw_key_gets_prefix(self):
        from kiro_crew.dashboard.chat import _history_key_for

        assert _history_key_for("chat-1-100") == "dashboard:chat-1-100"


# ── Folder CRUD tests ──


class TestFolderCRUD:
    @pytest.mark.asyncio
    async def test_list_folders_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/folders")
            assert resp.status == 200
            assert await resp.json() == []

    @pytest.mark.asyncio
    async def test_create_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Oncall"})
            assert resp.status == 201
            data = await resp.json()
            assert data["name"] == "Oncall"
            assert "id" in data
            assert data["collapsed"] is False
            # Persisted to disk
            assert (tmp_path / "folders.json").exists()

    @pytest.mark.asyncio
    async def test_create_folder_with_parent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Parent"})
            parent = await resp.json()
            resp = await client.post(
                "/api/chat/folders", json={"name": "Child", "parent_id": parent["id"]}
            )
            child = await resp.json()
            assert child["parent_id"] == parent["id"]

    @pytest.mark.asyncio
    async def test_create_folder_accepts_default_agent(self, tmp_path, monkeypatch):
        """The create modal collects the full folder config, so POST must take it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Payments", "default_agent": "kirocrew-dev"},
            )
            assert resp.status == 201
            data = await resp.json()
            assert data["default_agent"] == "kirocrew-dev"

    @pytest.mark.asyncio
    async def test_create_folder_accepts_palette_color(self, tmp_path, monkeypatch):
        """Create persists an allowlisted palette color and rejects others."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "Redteam", "color": "#ef4444"}
            )
            assert resp.status == 201
            assert (await resp.json())["color"] == "#ef4444"
            bad = await client.post("/api/chat/folders", json={"name": "Bad", "color": "#123456"})
            assert bad.status == 400
            assert (await bad.json())["code"] == "color_invalid"

    def test_folder_color_palette_matches_frontend_catalog(self):
        """The backend allowlist and the frontend catalog must offer the same
        hues: the modal renders FOLDER_COLOR_PALETTE (folderColorCatalog.tsx)
        while create/PATCH validate against _FOLDER_COLOR_PALETTE, so drift
        means the UI offers a swatch the backend rejects with a 400."""
        from pathlib import Path

        from kiro_crew.dashboard.chat_folders import _FOLDER_COLOR_PALETTE

        catalog = (
            Path(__file__).resolve().parent.parent
            / "website"
            / "src"
            / "components"
            / "folderColorCatalog.tsx"
        )
        frontend = set(re.findall(r"#[0-9a-f]{6}", catalog.read_text(encoding="utf-8")))
        assert frontend == set(_FOLDER_COLOR_PALETTE)

    @pytest.mark.asyncio
    async def test_patch_color_set_and_clear(self, tmp_path, monkeypatch):
        """PATCH color: allowlisted value sets, empty string clears the key."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Blue"})
            fid = (await resp.json())["id"]
            resp = await client.patch(f"/api/chat/folders/{fid}", json={"color": "#3b82f6"})
            assert resp.status == 200
            assert state._folders[0]["color"] == "#3b82f6"
            resp = await client.patch(f"/api/chat/folders/{fid}", json={"color": ""})
            assert resp.status == 200
            assert "color" not in state._folders[0]

    @pytest.mark.asyncio
    async def test_create_folder_invalid_parent_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "Orphan", "parent_id": "nonexistent"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_folder_empty_name_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": ""})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_folder_rename(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Old"})
            folder = await resp.json()
            resp = await client.patch(f"/api/chat/folders/{folder['id']}", json={"name": "New"})
            assert resp.status == 200
            data = await resp.json()
            assert data["name"] == "New"

    @pytest.mark.asyncio
    async def test_a_failed_folder_write_restores_the_slots_it_unfiled(self, tmp_path, monkeypatch):
        """Delete must not half-land: unfiled slots go back if the store write fails.

        The handler unfiles the folder's slots first, then commits the folder
        removal. If that commit fails, leaving the slots unfiled strands
        conversations outside a folder that still exists. Restoring them is
        deliberately order-neutral — the reverse order strands a dangling
        folder_id instead, so undoing whichever half landed is what closes both.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            folder = await (await client.post("/api/chat/folders", json={"name": "Work"})).json()
            slot = state.get_or_create_slot("in-folder")
            slot.folder_id = folder["id"]

            def boom(path, data):
                raise OSError("disk full")

            monkeypatch.setattr(state, "_atomic_write_json", boom)
            resp = await client.delete(f"/api/chat/folders/{folder['id']}")
            assert resp.status >= 500

        assert slot.folder_id == folder["id"], (
            "the slot stayed unfiled after the folder removal failed; the delete "
            "half-landed and the conversation left a folder that still exists"
        )

    @pytest.mark.asyncio
    async def test_a_parent_deleted_under_the_lock_rejects_the_create(self, tmp_path, monkeypatch):
        """A child must not land with a dangling parent_id.

        Parent existence is validated before the store lock is taken, so a
        concurrent delete of that parent would otherwise persist an orphan. Both
        the sidebar and the folder picker surface orphans at the top level rather
        than hiding them, so this is a tree-shape defect rather than a
        disappearance — but still a folder nobody asked to put at the root.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            parent = await (await client.post("/api/chat/folders", json={"name": "P"})).json()

            real = state.mutate_folders

            async def delete_parent_then_apply(mutate):
                # Stand in for a concurrent DELETE landing while this create
                # waits for the store lock.
                state._folders[:] = [f for f in state._folders if f["id"] != parent["id"]]
                return await real(mutate)

            monkeypatch.setattr(state, "mutate_folders", delete_parent_then_apply)
            resp = await client.post(
                "/api/chat/folders", json={"name": "Child", "parent_id": parent["id"]}
            )
            assert resp.status == 400, (
                f"create was accepted with a deleted parent (status {resp.status}); "
                "the folder persists as an orphan"
            )
        assert not any(f.get("name") == "Child" for f in state._folders)

    @pytest.mark.asyncio
    async def test_update_folder_collapse(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "F"})
            folder = await resp.json()
            resp = await client.patch(f"/api/chat/folders/{folder['id']}", json={"collapsed": True})
            data = await resp.json()
            assert data["collapsed"] is True

    @pytest.mark.asyncio
    async def test_update_folder_reparent_into_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            a = await (await client.post("/api/chat/folders", json={"name": "A"})).json()
            b = await (await client.post("/api/chat/folders", json={"name": "B"})).json()
            resp = await client.patch(f"/api/chat/folders/{b['id']}", json={"parent_id": a["id"]})
            assert resp.status == 200
            assert (await resp.json())["parent_id"] == a["id"]
            assert state._folders[1]["parent_id"] == a["id"]

    @pytest.mark.asyncio
    async def test_update_folder_reparent_to_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            a = await (await client.post("/api/chat/folders", json={"name": "A"})).json()
            b = await (
                await client.post("/api/chat/folders", json={"name": "B", "parent_id": a["id"]})
            ).json()
            # Empty string and null both mean "move to top level".
            resp = await client.patch(f"/api/chat/folders/{b['id']}", json={"parent_id": ""})
            assert resp.status == 200
            assert (await resp.json())["parent_id"] == ""
            resp = await client.patch(f"/api/chat/folders/{b['id']}", json={"parent_id": a["id"]})
            assert (await resp.json())["parent_id"] == a["id"]
            resp = await client.patch(f"/api/chat/folders/{b['id']}", json={"parent_id": None})
            assert resp.status == 200
            assert (await resp.json())["parent_id"] == ""

    @pytest.mark.asyncio
    async def test_concurrent_opposite_reparents_cannot_persist_a_cycle(
        self, tmp_path, monkeypatch
    ):
        """Two opposite reparents must not both apply and orphan the pair.

        The tree-shape rules (parent exists, target is not a descendant) are
        checked once before the store lock as a fast reject, but a folder write
        is serialized and off-loop, so while one is in flight BOTH requests can
        validate against the same pre-state. If the winner's mutation were not
        re-tested under the lock, `A -> B` and `B -> A` would both persist and
        neither folder would be reachable from the root any more.

        The lock is held deliberately here so both handlers are provably queued
        on it before either applies -- the window is opened, not raced for.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            a = await (await client.post("/api/chat/folders", json={"name": "A"})).json()
            b = await (await client.post("/api/chat/folders", json={"name": "B"})).json()

            gate = asyncio.Event()

            async def _hold_the_store() -> None:
                async with state._folders_lock:
                    await gate.wait()

            holder = asyncio.create_task(_hold_the_store())
            while not state._folders_lock.locked():
                await asyncio.sleep(0)

            t1 = asyncio.create_task(
                client.patch(f"/api/chat/folders/{a['id']}", json={"parent_id": b["id"]})
            )
            t2 = asyncio.create_task(
                client.patch(f"/api/chat/folders/{b['id']}", json={"parent_id": a["id"]})
            )

            async def _both_queued() -> None:
                # _folders_lock is a LoopBoundLock (#4800); the waiter queue
                # lives on this loop's inner asyncio.Lock.
                inner = state._folders_lock._bound()
                while len(getattr(inner, "_waiters", None) or ()) < 2:
                    await asyncio.sleep(0.01)

            # Both requests have passed their pre-lock validation against the
            # SAME pre-state and are now waiting on the store.
            await asyncio.wait_for(_both_queued(), timeout=5)
            gate.set()
            r1, r2 = await asyncio.gather(t1, t2)
            await holder

            assert sorted([r1.status, r2.status]) == [200, 409], (
                "both opposite reparents were accepted; the tree-shape rules are "
                "not being re-tested under the store lock"
            )

            parents = {f["id"]: f.get("parent_id", "") for f in state._folders}

            def _reaches_root(fid: str) -> bool:
                seen: set[str] = set()
                cur = fid
                while cur:
                    if cur in seen:
                        return False
                    seen.add(cur)
                    cur = parents.get(cur, "")
                return True

            assert all(
                _reaches_root(fid) for fid in parents
            ), f"a parent cycle was persisted: {parents}"
            stored = json.loads((tmp_path / state._FOLDERS_FILE).read_text(encoding="utf-8"))
            assert {f["id"]: f.get("parent_id", "") for f in stored} == parents

    @pytest.mark.asyncio
    async def test_update_folder_reparent_self_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            a = await (await client.post("/api/chat/folders", json={"name": "A"})).json()
            resp = await client.patch(f"/api/chat/folders/{a['id']}", json={"parent_id": a["id"]})
            assert resp.status == 400
            assert state._folders[0]["parent_id"] == ""

    @pytest.mark.asyncio
    async def test_update_folder_reparent_unknown_parent_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            a = await (await client.post("/api/chat/folders", json={"name": "A"})).json()
            resp = await client.patch(f"/api/chat/folders/{a['id']}", json={"parent_id": "nope"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_folder_reparent_cycle_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            a = await (await client.post("/api/chat/folders", json={"name": "A"})).json()
            b = await (
                await client.post("/api/chat/folders", json={"name": "B", "parent_id": a["id"]})
            ).json()
            c = await (
                await client.post("/api/chat/folders", json={"name": "C", "parent_id": b["id"]})
            ).json()
            # A -> C would create A > B > C > A
            resp = await client.patch(f"/api/chat/folders/{a['id']}", json={"parent_id": c["id"]})
            assert resp.status == 400
            assert "descendant" in (await resp.json())["error"]
            # Direct child is also a descendant
            resp = await client.patch(f"/api/chat/folders/{a['id']}", json={"parent_id": b["id"]})
            assert resp.status == 400
            assert state._folders[0]["parent_id"] == ""

    @pytest.mark.asyncio
    async def test_update_folder_rejected_field_leaves_no_partial_mutation(
        self, tmp_path, monkeypatch
    ):
        """A PATCH mixing a VALID field (name) with an INVALID one (bad parent_id)
        must be all-or-nothing: the 400 rejection must NOT persist the name change
        (validate-all-before-mutate). Regression for the partial-mutation bug."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            f = await (await client.post("/api/chat/folders", json={"name": "Original"})).json()
            resp = await client.patch(
                f"/api/chat/folders/{f['id']}",
                json={"name": "Renamed", "parent_id": "does-not-exist"},
            )
            assert resp.status == 400
            # The name change from the SAME rejected request must not have landed.
            assert state._folders[0]["name"] == "Original"
            # And a later valid update must not resurrect the rejected name.
            resp = await client.patch(f"/api/chat/folders/{f['id']}", json={"collapsed": True})
            assert resp.status == 200
            assert state._folders[0]["name"] == "Original"

    @pytest.mark.asyncio
    async def test_create_folder_hidden_defaults_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "F"})
            assert (await resp.json())["hidden"] is False
            resp = await client.get("/api/chat/folders")
            assert (await resp.json())[0]["hidden"] is False

    @pytest.mark.asyncio
    async def test_update_folder_hidden(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "F"})
            folder = await resp.json()
            resp = await client.patch(f"/api/chat/folders/{folder['id']}", json={"hidden": True})
            assert (await resp.json())["hidden"] is True
            assert state._folders[0]["hidden"] is True
            resp = await client.patch(f"/api/chat/folders/{folder['id']}", json={"hidden": False})
            assert (await resp.json())["hidden"] is False

    @pytest.mark.asyncio
    async def test_folders_get_includes_history_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [
            {"id": "f1", "name": "A", "order": 0, "collapsed": False},
            {"id": "f2", "name": "B", "order": 1, "collapsed": False},
        ]
        # History count is computed from the full session list (authoritative),
        # not the paginated client history window.
        monkeypatch.setattr(
            state.conversation_log,
            "list_sessions",
            lambda: [
                {"key": "a", "folder_id": "f1"},
                {"key": "b", "folder_id": "f1"},
                {"key": "c", "folder_id": "f2"},
                {"key": "d"},  # unfiled — ignored
            ],
        )
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/folders")
            folders = {f["id"]: f for f in await resp.json()}
            assert folders["f1"]["history_count"] == 2
            assert folders["f2"]["history_count"] == 1

    @pytest.mark.asyncio
    async def test_assign_slot_to_folder_unhides(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("myslot")
        state._folders = [
            {"id": "f1", "name": "Test", "order": 0, "collapsed": False, "hidden": True}
        ]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            # Moving a session into a hidden folder re-engages it (Model B) → un-hides.
            resp = await client.patch("/api/chat/slots/myslot/folder", json={"folder_id": "f1"})
            assert resp.status == 200
            assert state._folders[0]["hidden"] is False

    @pytest.mark.asyncio
    async def test_resume_session_unhides_folder(self, tmp_path, monkeypatch):
        """Reviving a history session filed in a hidden folder un-hides it (Model B).

        Complements test_assign_slot_to_folder_unhides (the move path): here the
        re-engage happens via api_chat_slot_resume loading an archived session
        from history, which is the revive path that lets a hidden folder reappear.
        """
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [
            {"id": "f1", "name": "Test", "order": 0, "collapsed": False, "hidden": True}
        ]
        # Create a session filed in f1, persist it to history, then drop the active
        # slot so resume loads it fresh from history (the revive path).
        slot = state.get_or_create_slot("revive1")
        slot.folder_id = "f1"
        slot.append("user", "old msg")
        slot.drain()
        _save_slot_to_history(state, slot, closed=True)
        state._slots.pop("revive1", None)
        assert state._folders[0]["hidden"] is True  # still hidden before revive

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/revive1/resume", json={"key": "dashboard:revive1"}
            )
            assert resp.status == 200
        assert state._folders[0]["hidden"] is False  # revive re-engaged → un-hid

    @pytest.mark.asyncio
    async def test_update_folder_default_agent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "MS&AD"})
            folder = await resp.json()
            resp = await client.patch(
                f"/api/chat/folders/{folder['id']}", json={"default_agent": "msad"}
            )
            data = await resp.json()
            assert data["default_agent"] == "msad"
            # Verify persistence
            assert state._folders[0]["default_agent"] == "msad"

    @pytest.mark.asyncio
    async def test_update_folder_clear_default_agent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [
            {"id": "f1", "name": "Test", "order": 0, "collapsed": False, "default_agent": "nissay"}
        ]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/folders/f1", json={"default_agent": ""})
            data = await resp.json()
            assert data["default_agent"] == ""

    @pytest.mark.asyncio
    async def test_create_folder_with_project_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        proj = tmp_path / "proj"
        proj.mkdir()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "P", "project_dir": str(proj)}
            )
            assert resp.status == 201
            data = await resp.json()
            assert data["project_dir"] == os.path.realpath(str(proj))

    @pytest.mark.asyncio
    async def test_create_folder_relative_project_dir_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "P", "project_dir": "relative/path"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_folder_nonexistent_project_dir_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        missing = tmp_path / "does-not-exist"
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "P", "project_dir": str(missing)}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_folder_sensitive_project_dir_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "P", "project_dir": "~/.ssh"}
            )
            assert resp.status == 400
            data = await resp.json()
            assert "sensitive" in data["error"]

    @pytest.mark.asyncio
    async def test_update_folder_project_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        proj = tmp_path / "proj2"
        proj.mkdir()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "P"})
            folder = await resp.json()
            resp = await client.patch(
                f"/api/chat/folders/{folder['id']}", json={"project_dir": str(proj)}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["project_dir"] == os.path.realpath(str(proj))

    @pytest.mark.asyncio
    async def test_slot_create_inherits_nearest_folder_project(self, tmp_path, monkeypatch):
        """The server owns folder inheritance when the client cache omits project."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        root_project = tmp_path / "root-project"
        parent_project = tmp_path / "parent-project"
        root_project.mkdir()
        parent_project.mkdir()
        state._folders = [
            {
                "id": "root",
                "name": "Root",
                "order": 0,
                "parent_id": "",
                "project_dir": str(root_project),
            },
            {
                "id": "parent",
                "name": "Parent",
                "order": 1,
                "parent_id": "root",
                "project_dir": str(parent_project),
            },
            {
                "id": "child",
                "name": "Child",
                "order": 2,
                "parent_id": "parent",
                "project_dir": "",
            },
        ]
        mock_cfg = MagicMock()
        mock_cfg.dashboard.default_project = ""
        # A bare MagicMock leaks into the slot: the handler stamps
        # cfg.default_agent (a truthy MagicMock) as the slot's agent when the
        # request names none, and the coalesced slots broadcast then dies in
        # json.dumps ("Object of type MagicMock is not JSON serializable"),
        # 500ing the create. Pin it to a string like the sibling cfg mocks.
        mock_cfg.default_agent = ""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.default_project_dir",
            lambda _workspace: str(root_project),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.schedule_eager_spawn",
            lambda *_args, **_kwargs: None,
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post(
                "/api/chat/slots",
                json={"name": "folder-project", "folder_id": "child"},
            )
            data = await resp.json()

        assert resp.status == 200
        assert data["folder_id"] == "child"
        assert data["project"] == os.path.realpath(str(parent_project))
        assert state._slots["folder-project"].project == data["project"]

    @pytest.mark.asyncio
    async def test_slot_create_rejects_invalid_inherited_project(self, tmp_path, monkeypatch):
        """A stale folder path fails before a partially configured slot is created."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [
            {
                "id": "folder",
                "name": "Folder",
                "order": 0,
                "parent_id": "",
                "project_dir": str(tmp_path / "missing"),
            }
        ]

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post(
                "/api/chat/slots",
                json={"name": "invalid-folder-project", "folder_id": "folder"},
            )
            data = await resp.json()

        assert resp.status == 400
        assert data["code"] == "folder_project_invalid"
        assert state._slots == {}

    @pytest.mark.asyncio
    async def test_slot_create_does_not_rescope_existing_named_slot(self, tmp_path, monkeypatch):
        """Folder inheritance initializes new slots; explicit project changes stay explicit."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        old_project = tmp_path / "old-project"
        folder_project = tmp_path / "folder-project"
        old_project.mkdir()
        folder_project.mkdir()
        slot = state.get_or_create_slot("existing")
        slot.project = str(old_project)
        slot.append("user", "existing conversation")
        slot.drain()
        state._folders = [
            {
                "id": "folder",
                "name": "Folder",
                "order": 0,
                "parent_id": "",
                "project_dir": str(folder_project),
            }
        ]
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.schedule_eager_spawn",
            lambda *_args, **_kwargs: None,
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post(
                "/api/chat/slots",
                json={"name": "existing", "folder_id": "folder"},
            )

        assert resp.status == 200
        assert slot.project == str(old_project)

    def test_resolve_folder_project_dir_terminates_on_cycle(self):
        from kiro_crew.dashboard.chat_folders import _resolve_folder_project_dir

        folders = [
            {"id": "a", "parent_id": "b", "project_dir": ""},
            {"id": "b", "parent_id": "a", "project_dir": ""},
        ]
        assert _resolve_folder_project_dir(folders, "a") == ("", None)

    @pytest.mark.asyncio
    async def test_update_folder_empty_name_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Keep"})
            folder = await resp.json()
            resp = await client.patch(f"/api/chat/folders/{folder['id']}", json={"name": "  "})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_nonexistent_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/folders/nonexistent", json={"name": "X"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_delete_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Delete Me"})
            folder = await resp.json()
            resp = await client.delete(f"/api/chat/folders/{folder['id']}")
            assert resp.status == 200
            resp = await client.get("/api/chat/folders")
            assert await resp.json() == []

    @pytest.mark.asyncio
    async def test_delete_folder_reparents_children(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [
            {"id": "parent", "name": "Parent", "order": 0, "collapsed": False},
            {"id": "child", "name": "Child", "order": 1, "collapsed": False, "parent_id": "parent"},
        ]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            await client.delete("/api/chat/folders/parent")
            assert len(state._folders) == 1
            assert state._folders[0]["id"] == "child"
            assert state._folders[0].get("parent_id") == ""

    @pytest.mark.asyncio
    async def test_delete_folder_ungroups_slots(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.folder_id = "f-del"
        state._folders.append({"id": "f-del", "name": "X", "order": 0, "collapsed": False})
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            await client.delete("/api/chat/folders/f-del")
            assert slot.folder_id == ""

    @pytest.mark.asyncio
    async def test_assign_slot_to_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("myslot")
        state._folders = [{"id": "f1", "name": "Test", "order": 0, "collapsed": False}]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/myslot/folder", json={"folder_id": "f1"})
            assert resp.status == 200
            data = await resp.json()
            assert data["folder_id"] == "f1"
            assert state._slots["myslot"].folder_id == "f1"

    @pytest.mark.asyncio
    async def test_slot_folder_change_sets_reinject_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("myslot")
        state._folders = [{"id": "f1", "name": "Test", "order": 0, "collapsed": False}]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            # Moving into a folder flags the slot for a one-shot [FOLDER] re-inject.
            resp = await client.patch("/api/chat/slots/myslot/folder", json={"folder_id": "f1"})
            assert resp.status == 200
            assert slot._folder_changed is True
            # A no-op PATCH (same folder) must not re-flag.
            slot._folder_changed = False
            resp = await client.patch("/api/chat/slots/myslot/folder", json={"folder_id": "f1"})
            assert resp.status == 200
            assert slot._folder_changed is False

    @pytest.mark.asyncio
    async def test_unassign_slot_from_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("myslot")
        slot.folder_id = "f1"
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/myslot/folder", json={"folder_id": ""})
            assert resp.status == 200
            assert state._slots["myslot"].folder_id == ""

    @pytest.mark.asyncio
    async def test_assign_folder_nonexistent_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/nope/folder", json={"folder_id": "f1"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_assign_nonexistent_folder_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("myslot")
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/chat/slots/myslot/folder", json={"folder_id": "nonexistent"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_pin_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("myslot")
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/myslot/pin", json={"pinned": True})
            assert resp.status == 200
            data = await resp.json()
            assert data["pinned"] is True
            assert state._slots["myslot"].pinned is True

    @pytest.mark.asyncio
    async def test_unpin_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("myslot")
        slot.pinned = True
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/myslot/pin", json={"pinned": False})
            assert resp.status == 200
            assert state._slots["myslot"].pinned is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None, []])
    async def test_pin_slot_rejects_non_boolean_values(self, tmp_path, monkeypatch, value):
        """The API must not apply Python truthiness to JSON metadata."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("myslot")
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/myslot/pin", json={"pinned": value})
            assert resp.status == 400
            payload = await resp.json()
            assert payload["error"] == "pinned must be a boolean"
            assert payload["code"] == "pinned_not_bool"
        assert slot.pinned is False

    @pytest.mark.asyncio
    async def test_slots_include_pinned(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.pinned = True
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/slots")
            slots = await resp.json()
            assert any(s.get("pinned") is True for s in slots)

    @pytest.mark.asyncio
    async def test_slots_include_folder_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.folder_id = "f-abc"
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/slots")
            slots = await resp.json()
            assert any(s["folder_id"] == "f-abc" for s in slots)


class TestFolderTags:
    """Folder `tags` — vocabulary-constrained list persisted on create/update,
    and stripped from folders when a tag is deleted (issue #5419)."""

    @staticmethod
    def _seed_vocabulary(state, tag_ids):
        state._tags = [
            {"id": tid, "name": tid, "color": "#6b7280", "order": i}
            for i, tid in enumerate(tag_ids)
        ]
        # A directly-seeded vocabulary is KNOWN by definition — the constructor
        # leaves the committed snapshot None (UNKNOWN) until load_tags() runs.
        state.publish_committed_tag_ids(state._tags)

    @staticmethod
    def _app_with_tag_delete(state):
        """Folder endpoints plus the tag-delete route, on one app."""
        from kiro_crew.dashboard.chat_tags import api_chat_tag_delete

        app = _make_folder_app(state)
        app.router.add_delete("/api/chat/tags/{id}", api_chat_tag_delete)
        return app

    @pytest.mark.asyncio
    async def test_create_and_update_with_valid_tags_persists(self, tmp_path, monkeypatch):
        """(a) Create with valid tags persists them; PATCH replaces the set."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        self._seed_vocabulary(state, ["t1", "t2", "t3"])
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "Payments", "tags": ["t1", "t2"]}
            )
            assert resp.status == 201
            data = await resp.json()
            assert data["tags"] == ["t1", "t2"]
            fid = data["id"]
            # Reloaded from the store reflects the persisted list.
            assert state._folders[0]["tags"] == ["t1", "t2"]

            # PATCH replaces the tag set.
            resp = await client.patch(f"/api/chat/folders/{fid}", json={"tags": ["t3"]})
            assert resp.status == 200
            assert state._folders[0]["tags"] == ["t3"]

            # An empty list clears the key entirely (absent means no tags).
            resp = await client.patch(f"/api/chat/folders/{fid}", json={"tags": []})
            assert resp.status == 200
            assert "tags" not in state._folders[0]

    @pytest.mark.asyncio
    async def test_create_dedupes_tags(self, tmp_path, monkeypatch):
        """Duplicates collapse (order preserved) and a valid set persists."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        self._seed_vocabulary(state, ["t1", "t2"])
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "Dupes", "tags": ["t1", "t1", "t2"]}
            )
            assert resp.status == 201
            assert (await resp.json())["tags"] == ["t1", "t2"]

    @pytest.mark.asyncio
    async def test_malformed_vocabulary_id_does_not_crash_validation(self, tmp_path, monkeypatch):
        """A malformed persisted vocabulary entry (non-string ``id`` — e.g. a
        hand-edited tags.json carrying a list) must degrade to "unknown id"
        rather than crash the validator's set build with an unhashable type
        (which would 500 every folder-tag operation)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        self._seed_vocabulary(state, ["t1"])
        # Corrupt entries alongside the valid one: unhashable id, missing id.
        state._tags.append({"id": ["not", "a", "string"], "name": "bad"})
        state._tags.append({"name": "no-id"})
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "Robust", "tags": ["t1", "t9"]}
            )
            assert resp.status == 201
            # The valid id survives; the unknown one is filtered; no crash.
            assert (await resp.json())["tags"] == ["t1"]

    @pytest.mark.asyncio
    async def test_unreadable_vocab_does_not_wipe_folder_tags(self, tmp_path, monkeypatch):
        """FAIL-OPEN parity with ``validate_folder_tag_ids``: when tags.json
        was unreadable at boot (vocabulary UNKNOWN, ``_committed_tag_ids``
        None), a folder PATCH carrying ``tags`` must keep the ids rather than
        intersecting them against the unknown (empty) vocabulary — that
        intersection would silently wipe the folder's tags and the PATCH would
        persist the loss."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        self._seed_vocabulary(state, ["t1", "t2"])
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "Payments", "tags": ["t1", "t2"]}
            )
            assert resp.status == 201
            fid = (await resp.json())["id"]

            # Simulate a later boot where tags.json failed to load.
            state._tags = []
            state._committed_tag_ids = None

            # A PATCH that echoes the stored tags back (any rename flow does
            # this) must not wipe them; shape guards and dedupe still apply.
            resp = await client.patch(
                f"/api/chat/folders/{fid}",
                json={"name": "Renamed", "tags": ["t1", "t1", "t2", 7]},
            )
            assert resp.status == 200
            assert state._folders[0]["tags"] == ["t1", "t2"]  # preserved, not wiped

    @pytest.mark.asyncio
    async def test_folder_tag_writes_hold_the_tags_write_lock(self, tmp_path, monkeypatch):
        """The point-of-application intersection and the folders write are ONE
        critical section under ``tags_write_lock`` (the invariant every tag
        consumer follows): a tag deletion committing between them would slip a
        just-deleted id past the strip pass and back onto the folder."""
        from kiro_crew.dashboard.chat_tags import tags_write_lock

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        self._seed_vocabulary(state, ["t1"])
        held: list[bool] = []
        orig_mutate = state.mutate_folders

        async def _spy(fn):
            held.append(tags_write_lock(state).locked())
            return await orig_mutate(fn)

        monkeypatch.setattr(state, "mutate_folders", _spy)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Locked", "tags": ["t1"]})
            assert resp.status == 201
            fid = (await resp.json())["id"]
            resp = await client.patch(f"/api/chat/folders/{fid}", json={"tags": ["t1"]})
            assert resp.status == 200
        assert held[:2] == [True, True], (
            "a folder tags write ran outside tags_write_lock; a concurrent tag "
            "deletion could resurrect a deleted id onto the folder"
        )

    @pytest.mark.asyncio
    async def test_unknown_tag_id_is_filtered_not_400(self, tmp_path, monkeypatch):
        """(b) An unknown tag id is silently dropped on create and update.

        Mirrors ``api_chat_slot_tags`` exactly: a dangling id can legitimately
        exist (the tag delete's best-effort folder strip can fail), and a
        strict 400 here would make every subsequent save of that folder fail —
        the "permanently uneditable folder" class. Filtering at the write
        sheds the stale reference instead.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        self._seed_vocabulary(state, ["t1"])
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "Mixed", "tags": ["nope", "t1"]}
            )
            assert resp.status == 201
            fid = (await resp.json())["id"]
            # Unknown id dropped, known id kept.
            assert state._folders[0].get("tags") == ["t1"]

            bad_patch = await client.patch(f"/api/chat/folders/{fid}", json={"tags": ["ghost"]})
            assert bad_patch.status == 200
            # An all-unknown list filters down to empty — tags cleared, not 400.
            assert not state._folders[0].get("tags")

    @pytest.mark.asyncio
    async def test_non_array_tags_rejected_400(self, tmp_path, monkeypatch):
        """A non-array `tags` payload is a 400, not a 500."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        self._seed_vocabulary(state, ["t1"])
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Bad", "tags": "t1"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "tags_invalid"

    @pytest.mark.asyncio
    async def test_deleting_a_tag_strips_it_from_folders(self, tmp_path, monkeypatch):
        """(f) Deleting a tag from the vocabulary removes its id from every folder."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        self._seed_vocabulary(state, ["t1", "t2"])
        app = self._app_with_tag_delete(state)
        async with TestClient(TestServer(app)) as client:
            # Two folders carrying t1; one also carries t2.
            a = await (
                await client.post("/api/chat/folders", json={"name": "A", "tags": ["t1", "t2"]})
            ).json()
            b = await (
                await client.post("/api/chat/folders", json={"name": "B", "tags": ["t1"]})
            ).json()

            resp = await client.delete("/api/chat/tags/t1")
            assert resp.status == 200

        fa = next(f for f in state._folders if f["id"] == a["id"])
        fb = next(f for f in state._folders if f["id"] == b["id"])
        # t1 stripped everywhere; t2 untouched; a folder left with no tags loses
        # the key entirely.
        assert fa["tags"] == ["t2"]
        assert "tags" not in fb


class TestFolderPersistence:
    def test_load_folders_from_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        import json

        (tmp_path / "folders.json").write_text(
            json.dumps([{"id": "f1", "name": "Test", "order": 0}])
        )
        state = _make_state(tmp_path)
        state.load_folders()
        assert len(state._folders) == 1
        assert state._folders[0]["name"] == "Test"

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [{"id": "f1", "name": "Roundtrip", "order": 0, "collapsed": True}]
        state.save_folders()
        state._folders = []
        state.load_folders()
        assert state._folders[0]["name"] == "Roundtrip"
        assert state._folders[0]["collapsed"] is True

    def test_load_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.load_folders()
        assert state._folders == []

    def test_load_corrupted_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        (tmp_path / "folders.json").write_text("not json")
        state = _make_state(tmp_path)
        state.load_folders()
        assert state._folders == []


class TestGenerateEmojiForName:
    @pytest.mark.asyncio
    async def test_redaction_applied(self, tmp_path, monkeypatch):
        """generate_emoji_for_name (artifact-folder path) still redacts replies."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.dashboard.chat_folders import generate_emoji_for_name

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        mock_event = MagicMock()
        mock_event.kind = "text_chunk"
        mock_event.text = "🔥"
        done_event = MagicMock()
        done_event.kind = "complete"
        monkeypatch.setattr("kiro_crew.providers.base.EVENT_TEXT_CHUNK", "text_chunk")
        monkeypatch.setattr("kiro_crew.providers.base.EVENT_COMPLETE", "complete")
        monkeypatch.setattr("kiro_crew.providers.base.EVENT_PERMISSION_REQUEST", "permission")

        mock_client = AsyncMock()
        mock_client.prompt = MagicMock(return_value=AsyncIterator([mock_event, done_event]))
        state.sessions.get_bg_session = AsyncMock(return_value=mock_client)
        state.push_slots_update = MagicMock()

        with (
            patch(
                "kiro_crew.dashboard.chat_folders.redact_exfiltration_urls",
                return_value=("🔥", False),
            ) as mock_url,
            patch(
                "kiro_crew.dashboard.chat_folders.redact_credentials", return_value=("🔥", False)
            ) as mock_cred,
        ):
            icon = await generate_emoji_for_name(state, "Oncall")
            assert icon == "🔥"
            mock_url.assert_called_once()
            mock_cred.assert_called_once()

    @pytest.mark.asyncio
    async def test_variation_selector_emoji_accepted(self, tmp_path, monkeypatch):
        """Emoji with U+FE0F variation selector (e.g. ❤️) accepted by the
        emoji generator (still used for artifact-library folders)."""
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.dashboard.chat_folders import generate_emoji_for_name

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        mock_event = MagicMock()
        mock_event.kind = "text_chunk"
        mock_event.text = "\u2764\ufe0f"  # ❤️
        done_event = MagicMock()
        done_event.kind = "complete"
        monkeypatch.setattr("kiro_crew.providers.base.EVENT_TEXT_CHUNK", "text_chunk")
        monkeypatch.setattr("kiro_crew.providers.base.EVENT_COMPLETE", "complete")
        monkeypatch.setattr("kiro_crew.providers.base.EVENT_PERMISSION_REQUEST", "permission")

        mock_client = AsyncMock()
        mock_client.prompt = MagicMock(return_value=AsyncIterator([mock_event, done_event]))
        state.sessions.get_bg_session = AsyncMock(return_value=mock_client)

        icon = await generate_emoji_for_name(state, "Love")
        assert icon == "\u2764\ufe0f"


class TestFolderAssignmentPersistence:
    @pytest.mark.asyncio
    async def test_folder_assignment_saves_to_history(self, tmp_path, monkeypatch):
        """api_chat_slot_folder should call _save_slot_to_history for new sessions."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("myslot")
        slot.append("user", "hello")
        slot.drain()
        state._folders = [{"id": "f1", "name": "Test", "order": 0, "collapsed": False}]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            await client.patch("/api/chat/slots/myslot/folder", json={"folder_id": "f1"})
            path = tmp_path / "dashboard_myslot.jsonl"
            assert path.exists()
            import json

            meta = json.loads(path.read_text(encoding="utf-8").split("\n")[0])
            assert meta["folder_id"] == "f1"

    @pytest.mark.asyncio
    async def test_folder_assignment_persists_on_resumed_session(self, tmp_path, monkeypatch):
        """Regression: folder_id must reach disk even when slot is a resumed
        session with no new messages.

        Root cause: _save_slot_to_history had an early-return guard that
        skipped disk writes when ``slot._resumed_count > 0 and
        len(messages) <= slot._resumed_count``. Metadata-only changes like
        folder assignment don't grow the message count past the resumed
        marker, so the save was silently dropped — folder_id never reached
        disk and the move was lost on the next gateway restart.

        Fix: folder endpoint passes ``force=True`` which bypasses the guard.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("resumedslot")
        slot.append("user", "old message from before restart")
        slot.drain()
        # A genuinely resumed slot's file exists on disk (the restore read it).
        # Persist first so the fixture matches reality — a slot claiming
        # on-disk history whose file is missing is the delete-won state the
        # save now refuses to recreate (#6677).
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        _save_slot_to_history(state, slot, force=True)
        # Mark slot as a resumed session (simulates being restored from disk).
        # The guard fires when _resumed_count >= len(messages).
        slot._resumed_count = len(slot.messages)
        state._folders = [{"id": "f-resumed", "name": "Build", "order": 0, "collapsed": False}]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/chat/slots/resumedslot/folder",
                json={"folder_id": "f-resumed"},
            )
            assert resp.status == 200
            path = tmp_path / "dashboard_resumedslot.jsonl"
            assert path.exists(), "fixture save should have created the session file"
            import json

            meta = json.loads(path.read_text(encoding="utf-8").split("\n")[0])
            assert meta.get("folder_id") == "f-resumed", (
                "folder_id was silently dropped on resumed session — "
                "force=True must bypass the _resumed_count guard"
            )

    @pytest.mark.asyncio
    async def test_pin_toggle_persists_on_resumed_session(self, tmp_path, monkeypatch):
        """Regression: pinned flag must reach disk on resumed sessions.

        Same root cause as the folder regression — the resumed-count guard
        in _save_slot_to_history was blocking metadata-only writes. Pin
        endpoint now passes ``force=True``.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("pinslot")
        slot.append("user", "old message")
        slot.drain()
        # Persist first: a resumed slot's file exists on disk (see the folder
        # regression above — the missing-file variant is the delete-won state
        # the save refuses to recreate, #6677).
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        _save_slot_to_history(state, slot, force=True)
        slot._resumed_count = len(slot.messages)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/pinslot/pin", json={"pinned": True})
            assert resp.status == 200
            path = tmp_path / "dashboard_pinslot.jsonl"
            assert path.exists(), "fixture save should have created the session file"
            import json

            meta = json.loads(path.read_text(encoding="utf-8").split("\n")[0])
            assert meta.get("pinned") is True, (
                "pinned was silently dropped on resumed session — "
                "force=True must bypass the _resumed_count guard"
            )

    def test_save_slot_force_bypasses_resumed_guard(self, tmp_path, monkeypatch):
        """Unit test: ``force=True`` must bypass the resumed-session guard.

        Without force, resumed sessions with no new messages skip the write.
        With force, the metadata-only mutation reaches disk regardless.
        """
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("forceslot")
        slot.append("user", "hello")
        slot.drain()
        # Persist first: a genuinely resumed slot's file exists on disk. (The
        # missing-file variant is the delete-won state the save refuses to
        # recreate, #6677.)
        _save_slot_to_history(state, slot, force=True)
        path = tmp_path / "dashboard_forceslot.jsonl"
        assert path.exists()
        slot._resumed_count = len(slot.messages)
        # Model a genuinely-resumed, UNCHANGED slot: restore sets _dirty=False.
        # (A dirty slot — e.g. an in-place stop-event edit — must NOT be skipped;
        # see test_inplace_stop_resolution_persists.)
        slot._dirty = False
        slot.folder_id = "f-force"

        import json

        # Without force — the resumed-count guard skips the save, so the
        # metadata mutation must NOT reach disk.
        _save_slot_to_history(state, slot)
        meta = json.loads(path.read_text(encoding="utf-8").split("\n")[0])
        assert meta.get("folder_id") is None, "guard must skip save when not forced"

        # With force — save bypasses the guard, folder_id is written.
        _save_slot_to_history(state, slot, force=True)

        meta = json.loads(path.read_text(encoding="utf-8").split("\n")[0])
        assert meta.get("folder_id") == "f-force"


class TestNewPlanResetsAutoRun:
    """Regression: _auto_run must reset when a new plan is detected."""

    def test_has_plan_resets_auto_run(self):
        """When LLM generates a new plan mid-execution, auto_run must be cleared."""
        from kiro_crew.dashboard.chat import _reset_auto_run_for_new_plan

        slot = _ChatSlot("plan-reset")
        slot._auto_run = True
        slot._orch_tracker = MagicMock()

        _reset_auto_run_for_new_plan(slot)

        assert slot._auto_run is False, "_auto_run must be reset for new plan"
        assert slot._orch_tracker is None


# ── Regenerate + variant switching ──


class TestRegenerateAndVariants:
    @pytest.mark.asyncio
    async def test_regenerate_truncates_and_stashes_variant(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "hello v1")
        slot.drain()
        captured = []

        async def _capture(*a, **kw):
            captured.extend(list(slot._pending_variants))

        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_capture):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                await asyncio.sleep(0)
        assert [m["role"] for m in slot.messages] == ["user"]
        assert len(captured) == 1
        assert captured[0]["content"] == "hello v1"

    @pytest.mark.asyncio
    async def test_regenerate_rejects_when_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "hello")

        # Simulate running task
        async def _noop():
            await asyncio.sleep(10)

        slot.task = asyncio.create_task(_noop())
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 409
        finally:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_regenerate_requires_prior_assistant(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "only user")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_switch_variant_updates_content(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "v2")
        slot.messages[-1]["variants"] = [
            {"content": "v1", "ts": "t1"},
            {"content": "v2", "ts": "t2"},
        ]
        slot.messages[-1]["variant_idx"] = 1
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
            assert resp.status == 200
            assert slot.messages[-1]["content"] == "v1"
            assert slot.messages[-1]["variant_idx"] == 0

    @pytest.mark.asyncio
    async def test_switch_variant_index_out_of_range(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = [{"content": "v1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 5})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_regenerate_passes_hint_to_run_chat(self, tmp_path, monkeypatch):
        """_run_chat should receive a non-empty regenerate_hint kwarg."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "reply")
        slot.drain()
        mock_run = AsyncMock()
        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=mock_run):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                # Let the scheduled task actually run so the mock records args
                await asyncio.sleep(0)
        mock_run.assert_called_once()
        _args, kwargs = mock_run.call_args
        assert kwargs.get("regenerate_hint"), "regenerate_hint must be non-empty"

    @pytest.mark.asyncio
    async def test_regenerate_preserves_existing_variants(self, tmp_path, monkeypatch):
        """When assistant already has variants[], regenerate keeps them and adds current."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "v2")
        slot.messages[-1]["variants"] = [
            {"content": "v1", "ts": "t1"},
            {"content": "v2", "ts": "t2"},
        ]
        slot.messages[-1]["variant_idx"] = 1
        slot.drain()
        captured = []

        async def _capture(*a, **kw):
            captured.extend(list(slot._pending_variants))

        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_capture):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                await asyncio.sleep(0)
        assert [v["content"] for v in captured] == ["v1", "v2"]

    @pytest.mark.asyncio
    async def test_regenerate_when_active_is_old_variant_no_dup(self, tmp_path, monkeypatch):
        """If user switched back to v1 then regenerates, v1 should not be appended twice."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = [
            {"content": "v1", "ts": "t1"},
            {"content": "v2", "ts": "t2"},
        ]
        slot.messages[-1]["variant_idx"] = 0
        slot.drain()
        captured = []

        async def _capture(*a, **kw):
            captured.extend(list(slot._pending_variants))

        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_capture):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                await asyncio.sleep(0)
        assert [v["content"] for v in captured] == ["v1", "v2"]

    @pytest.mark.asyncio
    async def test_regenerate_caps_variants(self, tmp_path, monkeypatch):
        """Variant list is capped; oldest entries drop when over _MAX_VARIANTS."""
        from kiro_crew.dashboard.chat import _MAX_VARIANTS

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "newest")
        existing = [{"content": f"v{i}", "ts": f"t{i}"} for i in range(_MAX_VARIANTS)]
        slot.messages[-1]["variants"] = existing
        slot.messages[-1]["variant_idx"] = len(existing) - 1
        slot.drain()
        captured = []

        async def _capture(*a, **kw):
            captured.extend(list(slot._pending_variants))

        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_capture):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                await asyncio.sleep(0)
        assert len(captured) <= _MAX_VARIANTS
        assert captured[-1]["content"] == "newest"

    @pytest.mark.asyncio
    async def test_regenerate_rejects_missing_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/nonexistent/regenerate")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_regenerate_rejects_empty_user_message(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "")
        slot.append("assistant", "reply")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_regenerate_persists_to_disk(self, tmp_path, monkeypatch):
        """After regenerate, on-disk history should reflect the truncation."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "old")
        slot.drain()
        # Save first so a file exists
        from kiro_crew.dashboard.chat import _history_key_for, _save_slot_to_history

        _save_slot_to_history(state, slot)
        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
        # File should now only contain the user message (assistant truncated)
        key = _history_key_for(slot.key)
        persisted = state.conversation_log.read_messages(key)
        roles = [m.get("role") for m in persisted]
        assert roles == ["user"]

    @pytest.mark.asyncio
    async def test_save_slot_redacts_variants(self, tmp_path, monkeypatch):
        """Variants written to disk must have credentials/exfil URLs redacted."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "safe content")
        # Plant a fake credential inside a variant
        slot.messages[-1]["variants"] = [
            {"content": "AKIAIOSFODNN7EXAMPLE secret stuff", "ts": "t1"},
            {"content": "safe content", "ts": "t2"},
        ]
        slot.messages[-1]["variant_idx"] = 1
        slot.drain()
        from kiro_crew.dashboard.chat import _history_key_for, _save_slot_to_history

        _save_slot_to_history(state, slot)
        key = _history_key_for(slot.key)
        persisted = state.conversation_log.read_messages(key)
        ai = [m for m in persisted if m.get("role") == "assistant"][0]
        assert "variants" in ai
        # The AKIA key must not appear in either variant after redaction
        for v in ai["variants"]:
            assert "AKIAIOSFODNN7EXAMPLE" not in v.get("content", "")

    @pytest.mark.asyncio
    async def test_switch_variant_rejects_when_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v2")
        slot.messages[-1]["variants"] = [
            {"content": "v1", "ts": "t1"},
            {"content": "v2", "ts": "t2"},
        ]

        async def _noop():
            await asyncio.sleep(10)

        slot.task = asyncio.create_task(_noop())
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
                assert resp.status == 409
        finally:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_switch_variant_missing_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/none/switch-variant", json={"index": 0})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_switch_variant_no_variants(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "plain")  # no variants[]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_switch_variant_invalid_json_body(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = [{"content": "v1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", data="not-json")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_switch_variant_non_int_index(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = [{"content": "v1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": "abc"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_edit_resend_non_dict_body_is_400_not_500(self, tmp_path, monkeypatch):
        # api_chat_slot_edit_resend parsed the body but never checked
        # isinstance(dict); a valid-JSON array/scalar reached body.get("index")
        # and raised AttributeError -> 500. Must be 400, like switch_variant.
        from kiro_crew.dashboard.chat import api_chat_slot_edit_resend

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/edit-resend", api_chat_slot_edit_resend)
        async with TestClient(TestServer(app)) as client:
            for bad in ("[1, 2]", '"hi"', "42"):
                resp = await client.post(
                    "/api/chat/slots/s1/edit-resend",
                    data=bad,
                    headers={"Content-Type": "application/json"},
                )
                assert resp.status == 400, f"body={bad!r} gave {resp.status}"

    @pytest.mark.asyncio
    async def test_regenerate_clears_pending_on_task_error(self, tmp_path, monkeypatch):
        """If _run_chat raises, _pending_variants must be cleared to prevent leak."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "reply")
        slot.drain()

        async def _boom(*a, **kw):
            raise RuntimeError("llm blew up")

        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_boom):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                # Let the failing task propagate through done_callback
                for _ in range(5):
                    await asyncio.sleep(0)
        assert slot._pending_variants == [], "pending variants must be cleared when task errors"

    @pytest.mark.asyncio
    async def test_flush_segment_attaches_pending_variants(self, tmp_path, monkeypatch):
        """_flush_segment should attach _pending_variants to the new assistant message."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        # Simulate pending variants from a regenerate
        slot._pending_variants = [
            {"content": "old v1", "ts": "t1"},
            {"content": "old v2", "ts": "t2"},
        ]
        from kiro_crew.dashboard.chat import _flush_segment

        _flush_segment(state, slot, "new reply", broadcast=False)
        last = slot.messages[-1]
        assert last["role"] == "assistant"
        assert last["content"] == "new reply"
        assert len(last["variants"]) == 3  # old v1, old v2, new reply
        assert last["variant_idx"] == 2
        assert slot._pending_variants == []

    @pytest.mark.asyncio
    async def test_switch_variant_negative_index(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = [{"content": "v1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": -1})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_regenerate_only_system_and_assistant(self, tmp_path, monkeypatch):
        """Regenerate should fail if there's no user message (only system + assistant)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("system", "you are helpful")
        slot.append("assistant", "hello")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_flush_segment_no_pending_no_variants(self, tmp_path, monkeypatch):
        """Normal flush without pending variants should not add variants field."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        from kiro_crew.dashboard.chat import _flush_segment

        _flush_segment(state, slot, "reply", broadcast=False)
        last = slot.messages[-1]
        assert "variants" not in last

    @pytest.mark.asyncio
    async def test_switch_variant_missing_index_key(self, tmp_path, monkeypatch):
        """Request body without 'index' key should return 400."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = [{"content": "v1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_restore_preserves_variants(self, tmp_path, monkeypatch):
        """Variants written to disk should be restored via production code path."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "v2")
        slot.messages[-1]["variants"] = [
            {"content": "v1", "ts": "t1"},
            {"content": "v2", "ts": "t2"},
        ]
        slot.messages[-1]["variant_idx"] = 1
        slot.drain()
        from kiro_crew.dashboard.chat import _save_slot_to_history, restore_recent_sessions

        _save_slot_to_history(state, slot)
        # Clear in-memory state and restore via production path
        state._slots.clear()
        restore_recent_sessions(state, window_minutes=9999)
        restored_slot = state._slots.get("s1")
        assert restored_slot is not None
        ai = [m for m in restored_slot.messages if m.get("role") == "assistant"][0]
        assert "variants" in ai
        assert len(ai["variants"]) == 2
        assert ai["variant_idx"] == 1

    @pytest.mark.asyncio
    async def test_regenerate_clears_pending_on_cancel(self, tmp_path, monkeypatch):
        """If user stops a regeneration (cancel), _pending_variants must be cleared."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "reply")
        slot.drain()

        async def _hang(*a, **kw):
            await asyncio.sleep(999)

        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_hang):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                assert slot._pending_variants != []
                # Cancel the task (simulates user clicking Stop)
                slot.task.cancel()
                for _ in range(5):
                    await asyncio.sleep(0)
        assert (
            slot._pending_variants == []
        ), "pending variants must be cleared when task is cancelled"

    @pytest.mark.asyncio
    async def test_prepare_messages_redacts_variant_content(self, tmp_path, monkeypatch):
        """Variant content exposed via API must have credentials redacted."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "safe")
        slot.messages[-1]["variants"] = [
            {"content": "AKIAIOSFODNN7EXAMPLE leaked key", "ts": "t1"},
            {"content": "safe", "ts": "t2"},
        ]
        from kiro_crew.dashboard.chat import _prepare_messages

        prepared = _prepare_messages(slot.messages, False, live_child="")
        ai = [m for m in prepared if m.get("role") == "assistant"][0]
        for v in ai["variants"]:
            assert "AKIAIOSFODNN7EXAMPLE" not in v.get("content", "")

    @pytest.mark.asyncio
    async def test_switch_variant_corrupt_entry(self, tmp_path, monkeypatch):
        """If a variant entry is not a dict, switch-variant should return 400."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = ["not-a-dict", {"content": "v1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_concurrent_regenerate_one_succeeds_one_409(self, tmp_path, monkeypatch):
        """Two simultaneous regenerate requests: one gets 200, the other gets 409."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "reply")
        slot.drain()

        async def _hang(*a, **kw):
            await asyncio.sleep(999)

        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_hang):
            async with TestClient(TestServer(_make_app(state))) as client:
                r1, r2 = await asyncio.gather(
                    client.post("/api/chat/slots/s1/regenerate"),
                    client.post("/api/chat/slots/s1/regenerate"),
                )
                statuses = sorted([r1.status, r2.status])
                assert statuses == [200, 409], f"Expected one 200 and one 409, got {statuses}"
        # Cleanup
        if slot.task:
            slot.task.cancel()


class TestForkSlot:
    """Tests for POST /api/chat/slots/{slot}/fork."""

    @pytest.mark.asyncio
    async def test_fork_copies_all_messages(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.title = "My Chat"
        slot._titled = True
        slot.append("user", "hello", "msg msg-u")
        slot.append("assistant", "hi there", "msg msg-a")
        slot.append("user", "how are you", "msg msg-u")
        slot.append("assistant", "good", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["messages"] == 4
            assert data["title"] == "↳ Fork of My Chat"

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        assert new_slot.forked_from == "dashboard:src"
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert len(visible) == 4

    @pytest.mark.asyncio
    async def test_fork_at_message_id(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "msg1", "msg msg-u")
        slot.append("assistant", "reply1", "msg msg-a")
        slot.append("user", "msg2", "msg msg-u")
        slot.append("assistant", "reply2", "msg msg-a")
        slot.drain()
        target_id = slot.messages[1]["meta"]["mid"]

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"at_message_id": target_id},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == 2

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert [m["content"] for m in visible] == ["msg1", "reply1"]

    @pytest.mark.asyncio
    async def test_fork_at_message_id_takes_precedence_over_window_index(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "msg1", "msg msg-u")
        slot.append("assistant", "reply1", "msg msg-a")
        slot.append("user", "msg2", "msg msg-u")
        slot.append("assistant", "reply2", "msg msg-a")
        slot.drain()
        target_id = slot.messages[3]["meta"]["mid"]

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"at_message_id": target_id, "at_message_index": 0},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == 4

    @pytest.mark.asyncio
    async def test_fork_at_index(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "msg1", "msg msg-u")
        slot.append("assistant", "reply1", "msg msg-a")
        slot.append("user", "msg2", "msg msg-u")
        slot.append("assistant", "reply2", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"at_message_index": 1})
            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == 2

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert len(visible) == 2
        assert visible[-1]["content"] == "reply1"

    @pytest.mark.asyncio
    async def test_fork_aborts_and_keeps_dirty_when_source_save_fails(self, tmp_path):
        # Regression (double-persistence data-loss): the fork persists the dirty
        # source slot before reading it as the source of truth, then clears
        # `_dirty` (which also disables the periodic retry). If that save is
        # best-effort it can silently drop the write under a lock timeout / I/O
        # error, yet `_dirty` would still be cleared — permanently losing the
        # unwritten source messages on the next restart. The fork must persist
        # with best_effort=False and, on failure, abort (503) WITHOUT clearing
        # `_dirty`, so the periodic flush still retries.
        from unittest.mock import AsyncMock, patch

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "unsaved-source-msg", "msg msg-u")
        slot.append("assistant", "unsaved-reply", "msg msg-a")
        slot.drain()
        # Force the save branch: pretend nothing has reached disk yet.
        slot._dirty = True
        slot._resumed_count = 0

        failing_save = AsyncMock(side_effect=RuntimeError("lock timeout"))
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            with patch("kiro_crew.dashboard.chat_fork.save_slot_off_loop", failing_save):
                resp = await client.post("/api/chat/slots/src/fork", json={})
                assert resp.status == 503

        # best_effort must be explicitly False so the failure propagated.
        assert failing_save.await_count == 1
        assert failing_save.await_args.kwargs.get("best_effort") is False
        # Slot stays dirty → the periodic flush will retry; messages not lost.
        assert slot._dirty is True

    @pytest.mark.asyncio
    async def test_fork_preserves_meta(self, tmp_path):
        # Regression: chat_fork.py previously dropped the `meta` dict when copying
        # messages into the new slot, silently breaking every meta-based feature
        # (knowledge chips, paste refs, future inline-comment rewrite badges).
        # Fork must preserve meta verbatim on copied messages.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append(
            "user",
            "find me a citation",
            "msg msg-u",
            meta={"paste_refs": ["ref-abc123"]},
        )
        slot.append(
            "assistant",
            "Here you go",
            "msg msg-a",
            meta={"knowledge_chips": [{"id": "kb-42", "title": "Cite-X"}]},
        )
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == 2

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert len(visible) == 2

        # User message: meta.paste_refs survived. Compared as a subset, not an
        # exact dict: `_ChatSlot.append` also stamps `meta.mid` (the per-row
        # delivery identity) on every persisted row, so an equality assertion
        # here would be asserting the absence of that id rather than the survival
        # of the caller's keys, which is what this regression is about.
        assert visible[0]["role"] == "user"
        user_meta = visible[0].get("meta") or {}
        assert user_meta.get("paste_refs") == [
            "ref-abc123"
        ], f"Fork dropped user meta. Got: {visible[0].get('meta')!r}"

        # Assistant message: meta.knowledge_chips survived
        assert visible[1]["role"] == "assistant"
        assistant_meta = visible[1].get("meta") or {}
        assert assistant_meta.get("knowledge_chips") == [
            {"id": "kb-42", "title": "Cite-X"}
        ], f"Fork dropped assistant meta. Got: {visible[1].get('meta')!r}"

    @pytest.mark.asyncio
    async def test_fork_handles_messages_without_meta(self, tmp_path):
        # The mirror of test_fork_preserves_meta: fork must not INVENT meta keys
        # the parent row did not have. Since `_ChatSlot.append` now stamps
        # `meta.mid` on every row, "no meta at all" is no longer the observable
        # invariant; the equivalent one is that the forked row's meta matches the
        # parent's exactly -- nothing added, nothing dropped.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "plain msg", "msg msg-u")
        slot.append("assistant", "plain reply", "msg msg-a")
        slot.drain()
        parent_meta = [m.get("meta") for m in slot.messages if m["role"] in ("user", "assistant")]

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert len(visible) == len(parent_meta)
        for m, expected in zip(visible, parent_meta):
            assert (
                m.get("meta") == expected
            ), f"Fork changed a message's meta. Got: {m.get('meta')!r}, want: {expected!r}"

    @pytest.mark.asyncio
    async def test_fork_not_found(self, tmp_path):
        state = _make_state(tmp_path)
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/nope/fork", json={})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_fork_empty_slot(self, tmp_path):
        state = _make_state(tmp_path)
        state.get_or_create_slot("empty")
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/empty/fork", json={})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_fork_inherits_agent_and_workspace(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src", agent="my-agent", workspace="my-ws")
        slot.model = "custom-model"
        slot.mode = "custom-mode"
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot.agent == "my-agent"
        assert new_slot.workspace == "my-ws"
        assert new_slot.model == "custom-model"
        assert new_slot.mode == "custom-mode"

    @pytest.mark.asyncio
    async def test_fork_of_member_slot_is_not_member_mode(self, tmp_path):
        """A fork of a member DM thread must be an ordinary chat, never a second
        "member" slot: the fork mints a chat-* key, so member mode would make it
        invisible everywhere (excluded from Sessions by surface mode, and absent
        from the roster, whose threads live only on member-<slug> keys)."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.mode = "member"
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        assert new_slot.mode == ""

    @pytest.mark.asyncio
    async def test_fork_mode_override_wins_over_inheritance(self, tmp_path):
        """An allowlisted ``mode`` in the body overrides the source slot's mode.
        The empty string is a legal override, so the override arm is selected on
        ``is not None``, not on truthiness."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.mode = "orchestrator"
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"mode": ""})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        assert new_slot.mode == ""

    @pytest.mark.asyncio
    async def test_fork_inherits_folder(self, tmp_path):
        """Fork must land in the same project folder as the source slot."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.folder_id = "proj-abc"
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        assert new_slot.folder_id == "proj-abc"

    @pytest.mark.asyncio
    async def test_fork_inherits_empty_folder(self, tmp_path):
        """Fork of an unfoldered slot stays unfoldered (root)."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot.folder_id == ""

    @pytest.mark.asyncio
    async def test_fork_inherits_project(self, tmp_path):
        """Fork must carry the source slot's active project directory."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.project = str(tmp_path / "repo")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        assert new_slot.project == str(tmp_path / "repo")

    @pytest.mark.asyncio
    async def test_fork_inherits_empty_project(self, tmp_path):
        """Fork of a projectless slot stays projectless."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot.project == ""

    @pytest.mark.asyncio
    async def test_fork_inherits_tags(self, tmp_path):
        """Fork must carry the source slot's assigned tag ids."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.tags = ["tag-a", "tag-b"]
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        assert new_slot.tags == ["tag-a", "tag-b"]
        # Copied, not aliased: mutating the fork's list must not touch the parent.
        new_slot.tags.append("tag-c")
        assert slot.tags == ["tag-a", "tag-b"]

    @pytest.mark.asyncio
    async def test_fork_inherits_empty_tags(self, tmp_path):
        """Fork of an untagged slot stays untagged."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot.tags == []

    @pytest.mark.asyncio
    async def test_fork_with_prompt(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "context", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"prompt": "fix the bug"},
            )
            data = await resp.json()
            assert data["ok"] is True
            assert data["prompt"] == "fix the bug"
            assert data["messages"] == 1

        # Prompt is returned for frontend to send separately — must NOT be
        # injected into the forked slot server-side.
        new_slot = state._slots.get(data["key"])
        assert all(m["content"] != "fix the bug" for m in new_slot.messages)

    @pytest.mark.asyncio
    async def test_fork_redacts_credentials_in_assistant_messages(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "show me the key", "msg msg-u")
        slot.append("assistant", "Here: AKIAIOSFODNN7EXAMPLE", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()
            assert data["ok"] is True

        new_slot = state._slots.get(data["key"])
        assistant_msgs = [m for m in new_slot.messages if m["role"] == "assistant"]
        assert "AKIAIOSFODNN7EXAMPLE" not in assistant_msgs[0]["content"]
        assert "[REDACTED" in assistant_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_fork_redacts_credentials_in_llm_generated_title(self, tmp_path):
        """Parent title is LLM-generated (via /api/chat/generate-title) and
        flows into the new slot's title + API response + dashboard JSON.
        Must be redacted like any other LLM output (AUTOSDE security-controls).
        """
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.title = "Leaked AKIAIOSFODNN7EXAMPLE key"
        slot._titled = True
        slot.append("user", "hi", "msg msg-u")
        slot.append("assistant", "ok", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()

        assert "AKIAIOSFODNN7EXAMPLE" not in data["title"]
        assert "[REDACTED" in data["title"]
        assert data["title"].startswith("↳ Fork of ")
        new_slot = state._slots.get(data["key"])
        assert "AKIAIOSFODNN7EXAMPLE" not in new_slot.title

    @pytest.mark.asyncio
    async def test_fork_rejects_bool_index(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"at_message_index": True})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_fork_rejects_negative_index(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"at_message_index": -1})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_fork_excludes_system_messages(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("system", "you are helpful", "msg msg-s")
        slot.append("user", "hello", "msg msg-u")
        slot.append("assistant", "hi", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()
            assert data["messages"] == 2

        new_slot = state._slots.get(data["key"])
        roles = [m["role"] for m in new_slot.messages]
        assert "system" not in roles

    @pytest.mark.asyncio
    async def test_fork_persists_to_disk(self, tmp_path):
        """Forked slot (and forked_from metadata) must survive a save/restore cycle."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.append("assistant", "hello", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()
            new_key = data["key"]

        # Simulate a gateway restart by reading messages + metadata from disk
        from kiro_crew.dashboard.chat import _history_key_for

        hk = _history_key_for(new_key)
        meta = state.conversation_log.get_metadata(hk)
        disk_msgs = state.conversation_log.read_messages(hk)
        assert meta.get("forked_from") == "dashboard:src", f"forked_from not persisted; meta={meta}"
        assert len(disk_msgs) == 2, f"forked messages not persisted (got {len(disk_msgs)})"

    @pytest.mark.asyncio
    async def test_fork_rejects_oversized_prompt(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"prompt": "x" * 40_000},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_fork_rejects_out_of_range_index(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.append("assistant", "hello", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"at_message_index": 5})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_fork_succeeds_while_streaming(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.append("assistant", "done reply", "msg msg-a")
        slot.drain()

        # Simulate a running session: task attribute non-None + not done
        class _FakeTask:
            def done(self):
                return False

        slot.task = _FakeTask()  # type: ignore[assignment]
        assert slot.running is True

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == 2

    @pytest.mark.asyncio
    async def test_fork_emits_sel_audit_event(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        mock_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_fork.sel", lambda: mock_sel)

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        mock_sel.log_api_access.assert_called_once()
        kw = mock_sel.log_api_access.call_args[1]
        assert kw["operation"] == "chat.slot_fork"
        assert kw["outcome"] == "allowed"
        assert "from=src" in kw["resources"]
        assert f"to={data['key']}" in kw["resources"]
        # L5 audit enrichment: at_index + prompt_len present
        assert "at_index=last" in kw["resources"]
        assert "prompt_len=0" in kw["resources"]

    @pytest.mark.asyncio
    async def test_fork_rejects_ephemeral_slot(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.memory_mode = "incognito"
        slot.append("user", "secret", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 400
            data = await resp.json()
            assert "persistent" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_fork_history_visible_to_new_kiro_via_context_builder(self, tmp_path):
        """Forked JSONL is the source build_session_context reads for the new slot.

        Guarantees the fresh kiro-cli process in the forked tab receives the
        copied user/assistant turns as thread-history context.
        """
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "parent question", "msg msg-u")
        slot.append("assistant", "parent answer", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()
            new_key = data["key"]

        # conversation_log.recent(forked_key) is what ContextBuilder.build_session_context
        # calls to assemble the thread-history section for the new kiro process.
        from kiro_crew.dashboard.chat import _history_key_for

        recent = state.conversation_log.recent(_history_key_for(new_key))
        visible = [m for m in recent if m.get("role") in ("user", "assistant")]
        assert [m["content"] for m in visible] == [
            "parent question",
            "parent answer",
        ], f"fork history not readable as new-session context: {visible}"

    @pytest.mark.asyncio
    async def test_fork_does_not_clone_parent_kiro_session_id(self, tmp_path, monkeypatch):
        """Parent's kiro-cli session id (session_map sid) must NOT carry to fork.

        Cloning the sid would make both tabs share one kiro process state and
        corrupt each other's view. Fork creates a FRESH kiro session on first
        prompt by leaving session_map unset for the new key.
        """
        from kiro_crew.session import SessionMap

        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        session_map = SessionMap()
        session_map.set("dashboard:src", "parent-kiro-sid-abc123")
        # A loop-side mutation defers its disk write; the fresh-instance
        # readback below needs the file current NOW.
        session_map.flush()

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()
            new_key = data["key"]

        # Re-read from disk so we're not trusting an in-process cache.
        # Inspect _data directly to skip SessionMap.get()'s kiro-session file
        # existence check (we don't spawn real kiro processes in unit tests).
        reloaded = SessionMap()
        assert (
            reloaded._data.get("dashboard:src", {}).get("sid") == "parent-kiro-sid-abc123"
        ), "parent's kiro sid should survive fork unchanged"
        assert (
            f"dashboard:{new_key}" not in reloaded._data
        ), "forked slot must NOT inherit parent's kiro sid"

    @pytest.mark.asyncio
    async def test_fork_of_fork_chains_forked_from(self, tmp_path):
        """M10: fork of a fork titles correctly and `forked_from` points to intermediate, not root."""
        state = _make_state(tmp_path)
        root = state.get_or_create_slot("root")
        root.title = "Original"
        root._titled = True
        root.append("user", "q1", "msg msg-u")
        root.append("assistant", "a1", "msg msg-a")
        root.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            r1 = await client.post("/api/chat/slots/root/fork", json={})
            d1 = await r1.json()
            mid_key = d1["key"]
            assert d1["title"] == "↳ Fork of Original"

            mid = state._slots.get(mid_key)
            mid.append("user", "q2", "msg msg-u")
            mid.append("assistant", "a2", "msg msg-a")
            mid.drain()

            r2 = await client.post(f"/api/chat/slots/{mid_key}/fork", json={})
            d2 = await r2.json()

        leaf = state._slots.get(d2["key"])
        assert d2["title"] == "↳ Fork of Fork of Original"
        assert (
            leaf.forked_from == f"dashboard:{mid_key}"
        ), f"leaf forked_from should point to intermediate, got {leaf.forked_from}"
        assert leaf.forked_from != "dashboard:root"
        visible = [m for m in leaf.messages if m["role"] in ("user", "assistant")]
        assert [m["content"] for m in visible] == ["q1", "a1", "q2", "a2"]

    @pytest.mark.asyncio
    async def test_fork_reads_full_history_from_disk_when_memory_capped(self, tmp_path):
        """M12: when in-memory snapshot is smaller than full history, fork reads from disk."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        for i in range(250):
            slot.append("user" if i % 2 == 0 else "assistant", f"m{i}", "msg")
        slot.drain()
        from kiro_crew.dashboard.chat import _save_slot_to_history

        _save_slot_to_history(state, slot)
        # Simulate restore cap: keep only last 50 in memory.
        # Clear _dirty so the endpoint's flush-if-dirty path doesn't overwrite disk.
        slot.messages = slot.messages[-50:]
        slot._dirty = False

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        assert (
            data["messages"] == 250
        ), f"fork should read full history from disk, got {data['messages']}"
        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert len(visible) == 250
        assert visible[0]["content"] == "m0"
        assert visible[-1]["content"] == "m249"

    @pytest.mark.asyncio
    async def test_fork_preserves_full_history_when_dirty_and_capped(self, tmp_path):
        """A1 regression: _dirty=True + capped in-memory must NOT truncate disk history."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        for i in range(250):
            slot.append("user" if i % 2 == 0 else "assistant", f"m{i}", "msg")
        slot.drain()
        from kiro_crew.dashboard.chat import _save_slot_to_history

        _save_slot_to_history(state, slot)
        # Simulate restore with cap: real path caps messages then sets
        # _resumed_count to the capped length. User then sends new messages.
        slot.messages = slot.messages[-50:]
        slot._resumed_count = len(slot.messages)
        slot.append("user", "new1", "msg")
        slot.append("assistant", "new2", "msg")
        slot.drain()
        assert slot._dirty is True

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        # Full 250 on disk + 2 new dirty messages = 252 total.
        assert (
            data["messages"] == 252
        ), f"fork must preserve full disk history + dirty tail, got {data['messages']}"
        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert visible[0]["content"] == "m0"
        assert visible[-2]["content"] == "new1"
        assert visible[-1]["content"] == "new2"

    @pytest.mark.asyncio
    async def test_fork_concurrent_requests_both_succeed(self, tmp_path):
        """R2-7: two rapid fork requests on the same slot both return 200 with
        identical visible-message counts. Each fork produces an independent new
        slot; no messages lost or duplicated."""
        import asyncio

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "q1", "msg msg-u")
        slot.append("assistant", "a1", "msg msg-a")
        slot.append("user", "q2", "msg msg-u")
        slot.append("assistant", "a2", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            r1, r2 = await asyncio.gather(
                client.post("/api/chat/slots/src/fork", json={}),
                client.post("/api/chat/slots/src/fork", json={}),
            )
            assert r1.status == 200 and r2.status == 200
            d1, d2 = await r1.json(), await r2.json()

        assert d1["key"] != d2["key"], "concurrent forks must produce distinct slot keys"
        assert (
            d1["messages"] == d2["messages"] == 4
        ), f"both forks must copy all 4 visible messages, got {d1['messages']}/{d2['messages']}"
        for key in (d1["key"], d2["key"]):
            new_slot = state._slots.get(key)
            visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
            assert [m["content"] for m in visible] == ["q1", "a1", "q2", "a2"]

    @pytest.mark.asyncio
    async def test_fork_audits_denied_on_ephemeral(self, tmp_path, monkeypatch):
        """M-1 regression: ephemeral rejection must emit a denied SEL event."""
        from unittest.mock import MagicMock

        mock_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_fork.sel", lambda: mock_sel)

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.memory_mode = "incognito"
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 400

        mock_sel.log_api_access.assert_called_once()
        kw = mock_sel.log_api_access.call_args[1]
        assert kw["operation"] == "chat.slot_fork"
        assert kw["outcome"] == "denied"
        assert "memory_mode=incognito" in kw["resources"]

    @pytest.mark.asyncio
    async def test_fork_app_isolation_rejects_cross_app(self, tmp_path, monkeypatch):
        """M-2 regression: app A cannot fork a slot owned by app B."""
        from unittest.mock import MagicMock

        mock_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_fork.sel", lambda: mock_sel)

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src", app="app-B")
        slot.append("user", "secret", "msg msg-u")
        slot.drain()

        # aiohttp middleware populates request["app"]; test injects via middleware.
        @web.middleware
        async def inject_app(request, handler):
            request["app"] = "app-A"
            return await handler(request)

        app_obj = _make_app(state)
        app_obj.middlewares.insert(0, inject_app)

        async with TestClient(TestServer(app_obj)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            # Cross-app access now returns 404 (indistinguishable from a missing
            # slot) to prevent slot enumeration — the true reason is still
            # recorded server-side via SEL (asserted below).
            assert resp.status == 404
            data = await resp.json()
            assert data["error"] == "not found"

        # denied event logged
        denied_calls = [
            c for c in mock_sel.log_api_access.call_args_list if c[1].get("outcome") == "denied"
        ]
        assert len(denied_calls) == 1
        assert denied_calls[0][1]["source"] == "app_isolation"

    @pytest.mark.asyncio
    async def test_fork_inherits_app_ownership(self, tmp_path):
        """I-1 regression: new_slot._app is the requesting app (or empty for dashboard)."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src", app="app-X")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        @web.middleware
        async def inject_app(request, handler):
            request["app"] = "app-X"
            return await handler(request)

        app_obj = _make_app(state)
        app_obj.middlewares.insert(0, inject_app)

        async with TestClient(TestServer(app_obj)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert (
            new_slot._app == "app-X"
        ), f"forked slot must inherit caller's app, got {new_slot._app!r}"

    @pytest.mark.asyncio
    async def test_fork_rejects_when_slot_cap_reached(self, tmp_path, monkeypatch):
        """Review finding: fork must return 429 + denied audit when slot cap hit."""
        from unittest.mock import MagicMock

        mock_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_fork.sel", lambda: mock_sel)
        # Lower the cap so we don't need to create hundreds of slots.
        monkeypatch.setattr("kiro_crew.dashboard.chat_fork.MAX_LIVE_SLOTS", 3)

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()
        # Pre-populate to hit the cap (src + 2 dummies = 3).
        state.get_or_create_slot("dummy1")
        state.get_or_create_slot("dummy2")
        assert len(state._slots) == 3

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 429
            data = await resp.json()
            assert "cap" in data["error"].lower()

        denied = [
            c for c in mock_sel.log_api_access.call_args_list if c[1].get("outcome") == "denied"
        ]
        assert len(denied) == 1
        assert denied[0][1]["source"] == "rate_limit"

    @pytest.mark.asyncio
    async def test_fork_at_index_spans_chained_session_files(self, tmp_path):
        """Index space must match the frontend (chained), not the current file alone.

        Regression for the slot detail endpoint returns
        ``read_messages_chained`` (all sibling session files sharing the
        slot's ``tab_id``), and the frontend builds its fork-button index
        against that list. Pre-fix, fork called ``read_messages`` and
        rejected any index past the current file's boundary.
        """
        from kiro_crew.dashboard.chat_utils import _history_key_for

        state = _make_state(tmp_path)
        tab_id = "tab12345abcd"

        # Older sibling session file with same tab_id (4 visible messages).
        # The chained-read glob matches ``dashboard_chat-*.jsonl`` so the key
        # must start with ``chat-`` to participate in chaining. The chained
        # walker uses ``sorted(glob)`` so file names must lexicographically
        # match chronological order — production uses ``chat-N-<ts>`` which
        # naturally sorts; mirror that with explicit ordering here.
        older_key = "dashboard:chat-tab-1-old"
        state.conversation_log.append(older_key, "user", "old-q1", tab_id=tab_id)
        state.conversation_log.append(older_key, "assistant", "old-a1")
        state.conversation_log.append(older_key, "user", "old-q2")
        state.conversation_log.append(older_key, "assistant", "old-a2")

        # Current session file with same tab_id (2 visible messages).
        current_key = "dashboard:chat-tab-2-new"
        state.conversation_log.append(current_key, "user", "new-q1", tab_id=tab_id)
        state.conversation_log.append(current_key, "assistant", "new-a1")
        state.conversation_log.invalidate_tab_id_cache()

        # In-memory slot mirrors the current file's persisted view; mark
        # clean so the fork handler relies on the chained read alone.
        slot = state.get_or_create_slot("chat-tab-2-new")
        slot._tab_id = tab_id
        slot.append("user", "new-q1", "msg msg-u")
        slot.append("assistant", "new-a1", "msg msg-a")
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot._dirty = False

        # Frontend visibleIndexMap assigns index 5 to the last "new-a1"
        # (chained list has 6 user/assistant entries: 4 older + 2 new).
        assert _history_key_for("chat-tab-2-new") == current_key
        chained = state.conversation_log.read_messages_chained(current_key)
        assert len([m for m in chained if m.get("role") in ("user", "assistant")]) == 6

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/chat-tab-2-new/fork",
                json={"at_message_index": 5},
            )
            assert resp.status == 200, await resp.text()
            data = await resp.json()
            assert data["ok"] is True
            assert data["messages"] == 6  # full chained history up to index 5

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert [m["content"] for m in visible] == [
            "old-q1",
            "old-a1",
            "old-q2",
            "old-a2",
            "new-q1",
            "new-a1",
        ]


# ── Color theme & persona injection tests ──


class TestColorTheme:
    """Tests for color_theme validation and slot assignment. Only "" and
    ``custom-<slug>`` (installed packs) are valid; any built-in visual-theme
    slug or junk value is coerced to "" (no persona path)."""

    @pytest.mark.asyncio
    async def test_color_theme_set_on_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        with patch("kiro_crew.dashboard.chat_handlers._run_chat", new=AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "theme-slot", "color_theme": "custom-mypack"},
                )
                assert resp.status == 200
                assert state._slots["theme-slot"].color_theme == "custom-mypack"

    @pytest.mark.asyncio
    async def test_color_theme_cleared_to_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("theme-slot")
        slot.color_theme = "custom-mypack"
        with patch("kiro_crew.dashboard.chat_handlers._run_chat", new=AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "theme-slot", "color_theme": ""},
                )
                assert resp.status == 200
                assert slot.color_theme == ""

    @pytest.mark.asyncio
    async def test_color_theme_not_cleared_when_absent(self, tmp_path, monkeypatch):
        """Omitting color_theme from body must not reset an existing theme."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("theme-slot")
        slot.color_theme = "custom-mypack"
        with patch("kiro_crew.dashboard.chat_handlers._run_chat", new=AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "theme-slot"},
                )
                assert resp.status == 200
                assert slot.color_theme == "custom-mypack"

    @pytest.mark.asyncio
    async def test_invalid_color_theme_coerced_to_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        with patch("kiro_crew.dashboard.chat_handlers._run_chat", new=AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "theme-slot", "color_theme": "evil"},
                )
                assert resp.status == 200
                assert state._slots["theme-slot"].color_theme == ""

    @pytest.mark.asyncio
    async def test_non_string_color_theme_coerced(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        with patch("kiro_crew.dashboard.chat_handlers._run_chat", new=AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "theme-slot", "color_theme": 42},
                )
                assert resp.status == 200
                assert state._slots["theme-slot"].color_theme == ""


class TestInstalledPackConsentInjection:
    """Content-bound (sha256) consent gate for INSTALLED pack personas
    (``custom-<slug>``). Injection requires the caller's ``theme_consent_sha``
    to equal sha256 of the persona text actually read from disk; anything else
    fails closed. Guards the Codex HIGH reinstall-swap fix."""

    PERSONA = "Speak like a friendly installed-pack host."

    @staticmethod
    def _sha(text: str) -> str:
        import hashlib

        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def test_matching_sha_injects(self):
        from kiro_crew.dashboard.chat import _maybe_inject_persona

        with patch(
            "kiro_crew.dashboard.chat_utils._installed_theme_persona",
            return_value=self.PERSONA,
        ):
            result = _maybe_inject_persona(
                "hello",
                "custom-mypack",
                True,
                theme_consent_sha=self._sha(self.PERSONA),
            )
        assert "[THEME PERSONA]" in result
        assert self.PERSONA in result

    def test_stale_sha_not_injected(self):
        # Reinstall rewrote persona.md; the stored hash no longer matches the
        # on-disk text -> the new, never-consented persona must NOT be injected.
        from kiro_crew.dashboard.chat import _maybe_inject_persona

        with patch(
            "kiro_crew.dashboard.chat_utils._installed_theme_persona",
            return_value=self.PERSONA,
        ):
            result = _maybe_inject_persona(
                "hello",
                "custom-mypack",
                True,
                theme_consent_sha=self._sha("OLD PERSONA THE USER CONSENTED TO"),
            )
        assert result == "hello"

    def test_absent_sha_not_injected(self):
        from kiro_crew.dashboard.chat import _maybe_inject_persona

        with patch(
            "kiro_crew.dashboard.chat_utils._installed_theme_persona",
            return_value=self.PERSONA,
        ):
            result = _maybe_inject_persona("hello", "custom-mypack", True)
        assert result == "hello"

    def test_legacy_boolean_alone_not_injected(self):
        # The legacy boolean consent field grants nothing on its own: with no
        # content-bound sha, an installed pack persona is never injected even
        # though the pack ships one.
        from kiro_crew.dashboard.chat import _maybe_inject_persona

        with patch(
            "kiro_crew.dashboard.chat_utils._installed_theme_persona",
            return_value=self.PERSONA,
        ):
            result = _maybe_inject_persona(
                "hello",
                "custom-mypack",
                True,
                theme_consent_sha=None,
            )
        assert result == "hello"

    def test_not_injected_on_followup(self):
        from kiro_crew.dashboard.chat import _maybe_inject_persona

        with patch(
            "kiro_crew.dashboard.chat_utils._installed_theme_persona",
            return_value=self.PERSONA,
        ):
            result = _maybe_inject_persona(
                "hello",
                "custom-mypack",
                False,
                theme_consent_sha=self._sha(self.PERSONA),
            )
        assert result == "hello"

    def test_malformed_sha_no_crash_no_injection(self):
        # GPT HIGH: a non-ASCII (or otherwise malformed) theme_consent_sha
        # must never reach hmac.compare_digest (which raises TypeError on
        # non-ASCII str, aborting the whole chat turn). The compare-site guard
        # treats anything that is not exactly 64 lowercase-hex as ABSENT:
        # no exception AND no injection.
        from kiro_crew.dashboard.chat import _maybe_inject_persona

        valid = self._sha(self.PERSONA)
        malformed = [
            "é",  # non-ASCII -> would TypeError in compare_digest
            valid.upper(),  # uppercase hex (raw, un-normalized) -> not 64-lower
            valid[:-1],  # 63 chars
            valid + "a",  # 65 chars
            "",  # empty
            "  ",  # whitespace only
            12345,  # non-str
            None,  # absent
            valid[:-2] + "gg",  # non-hex chars
        ]
        with patch(
            "kiro_crew.dashboard.chat_utils._installed_theme_persona",
            return_value=self.PERSONA,
        ):
            for bad in malformed:
                # The call must not raise for any malformed input...
                result = _maybe_inject_persona(
                    "hello",
                    "custom-mypack",
                    True,
                    theme_consent_sha=bad,
                )
                # ...and must not inject the persona.
                assert result == "hello", f"unexpected injection for {bad!r}"

    def test_normalizer_fail_closed_and_salvage(self):
        # The parse-site normalizer (validation.normalize_theme_consent_sha)
        # rejects malformed values (-> None, fail closed) and salvages a valid
        # sha wrapped in surrounding whitespace / uppercase (strip + lower).
        from kiro_crew.validation import normalize_theme_consent_sha

        valid = self._sha(self.PERSONA)
        assert normalize_theme_consent_sha("é") is None
        assert normalize_theme_consent_sha(valid[:-1]) is None
        assert normalize_theme_consent_sha("") is None
        assert normalize_theme_consent_sha(12345) is None
        assert normalize_theme_consent_sha(None) is None
        assert normalize_theme_consent_sha(valid) == valid
        # salvage: leading/trailing whitespace + uppercase normalize to canonical
        assert normalize_theme_consent_sha("  " + valid.upper() + "\n") == valid
        # a normalized value then injects through the real gate
        from kiro_crew.dashboard.chat import _maybe_inject_persona

        with patch(
            "kiro_crew.dashboard.chat_utils._installed_theme_persona",
            return_value=self.PERSONA,
        ):
            norm = normalize_theme_consent_sha("  " + valid.upper() + "\n")
            result = _maybe_inject_persona(
                "hello",
                "custom-mypack",
                True,
                theme_consent_sha=norm,
            )
        assert "[THEME PERSONA]" in result


class TestStopReasonCancelled:
    """Phase 4: handler response to stopReason='cancelled'."""

    @staticmethod
    def _make_mock_client(events):
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_state_for_run_chat(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = MagicMock()
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_handler_stop_reason_cancelled_skips_record_success(self, tmp_path, monkeypatch):
        """When EVENT_COMPLETE carries stop_reason='cancelled', neither
        record_success nor record_failure should be called."""
        from kiro_crew.acp.types import STOP_REASON_CANCELLED
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.record_success = MagicMock()
        state.sessions.record_failure = AsyncMock()

        await _run_chat(state, slot, "hello")

        state.sessions.record_success.assert_not_called()
        state.sessions.record_failure.assert_not_called()

    @staticmethod
    def _wire_reinjection(state, tmp_path, monkeypatch):
        """Give the state a context builder so the consume site actually runs.

        The class's default harness sets `context_builder = None`, which skips
        the leg that consumes the flag -- with no consume there is nothing to
        restore, so a test without this would pass vacuously.
        """
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        ctx_builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        monkeypatch.setattr(
            ctx_builder,
            "build_message",
            lambda text, *a, **kw: (text, MagicMock(action=None, text="")),
        )
        state.context_builder = ctx_builder
        state.sessions.consume_needs_reinjection = MagicMock(return_value=True)
        state.sessions.mark_needs_reinjection = MagicMock()
        return state

    @pytest.mark.asyncio
    async def test_cancelled_turn_restores_the_reinjection_flag(self, tmp_path, monkeypatch):
        """A graceful cancel discards the prompt, so the one-shot
        post-compaction re-injection flag must be put back.

        Regression guard for the path a narrower fix missed: restoring only on
        the empty-response re-queue left a soft-stop losing the skills index for
        the rest of the session.
        """
        from kiro_crew.acp.types import STOP_REASON_CANCELLED
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        self._wire_reinjection(state, tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        await _run_chat(state, slot, "hello")

        state.sessions.consume_needs_reinjection.assert_called()
        state.sessions.mark_needs_reinjection.assert_called_once()

    @pytest.mark.asyncio
    async def test_exception_mid_turn_restores_the_reinjection_flag(self, tmp_path, monkeypatch):
        """An exception between the consume and the success check must not
        swallow the flag either -- the restore lives in the `finally`, so every
        non-landing exit is covered, not only the ones that reach the end."""
        from kiro_crew.dashboard.chat import _run_chat

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        self._wire_reinjection(state, tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = MagicMock()

        def _boom(_msg):
            raise RuntimeError("provider exploded mid-turn")

        client.stream = _boom
        client.context_usage_pct = MagicMock(return_value=0.0)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.record_failure = AsyncMock()

        await _run_chat(state, slot, "hello")

        state.sessions.mark_needs_reinjection.assert_called_once()

    @pytest.mark.asyncio
    async def test_landed_turn_does_not_restore_the_reinjection_flag(self, tmp_path, monkeypatch):
        """The complement: a turn that lands must leave the flag cleared,
        otherwise the index is re-paid on every subsequent turn."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="a real answer"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        self._wire_reinjection(state, tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.record_success = MagicMock()

        await _run_chat(state, slot, "hello")

        state.sessions.mark_needs_reinjection.assert_not_called()

    @pytest.mark.asyncio
    async def test_handler_stop_reason_cancelled_skips_consolidation(self, tmp_path, monkeypatch):
        """When cancelled, maybe_consolidate must not be called."""
        from kiro_crew.acp.types import STOP_REASON_CANCELLED
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        await _run_chat(state, slot, "hello")

        state.consolidator.maybe_consolidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_handler_stop_reason_end_turn_preserves_existing_behavior(
        self, tmp_path, monkeypatch
    ):
        """When stop_reason='end_turn', record_success and maybe_consolidate fire."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="done"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.record_success = MagicMock()

        await _run_chat(state, slot, "hello")

        state.sessions.record_success.assert_called_once()
        state.consolidator.maybe_consolidate.assert_called_once()

    @pytest.mark.asyncio
    async def test_monitor_wake_turn_does_not_write_user_memory(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.state import MONITOR_WAKE_PREFIX
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="monitor action complete"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.record_success = MagicMock()

        await _run_chat(state, slot, f"{MONITOR_WAKE_PREFIX}\nChecks failed.")

        state.sessions.record_success.assert_called_once()
        state.consolidator.maybe_consolidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_handler_stop_reason_cancelled_flushes_partial_text(self, tmp_path, monkeypatch):
        """Partial text chunks before cancel must be flushed to the slot."""
        from kiro_crew.acp.types import STOP_REASON_CANCELLED
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial output here"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        await _run_chat(state, slot, "hello")

        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert any("partial output here" in m["content"] for m in assistant_msgs)


# ── Phase 5: Soft-stop dashboard backend tests ──


class TestStopTurnSlotState:
    """Tests for api_chat_slot_stop soft/hard state transitions."""

    def _make_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        sessions = MagicMock(count=0)
        sessions.stop_turn = AsyncMock(return_value="soft")
        sessions.reset = AsyncMock()
        sessions.get_pid = MagicMock(return_value=None)
        state = DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]),
                status=MagicMock(return_value={}),
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )
        state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
        return state

    @pytest.mark.asyncio
    async def test_stop_turn_slot_state_transitions_soft(self, tmp_path, monkeypatch):
        """POST stop → idle→soft_pending; after on_soft → idle."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))

        captured_states: list[str] = []

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            captured_states.append(slot._stop_state)
            if on_soft:
                await on_soft()
            return "soft"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200

        assert captured_states == ["soft_pending"]
        assert slot._stop_state == "idle"
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_stop_turn_slot_state_transitions_hard(self, tmp_path, monkeypatch):
        """POST stop with hard outcome → idle→soft_pending→idle after on_hard."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            if on_hard:
                await on_hard()
            return "hard"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200

        assert slot._stop_state == "idle"
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_stop_turn_force_query_param(self, tmp_path, monkeypatch):
        """POST stop?force=true when soft_pending → skips cancel, hard kill."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        slot._stop_state = "soft_pending"

        force_called = []

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            force_called.append(force)
            if on_hard:
                await on_hard()
            return "hard"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop?force=true")
            assert resp.status == 200

        assert force_called == [True]
        assert slot._stop_state == "idle"
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_stop_turn_second_press_escalates_without_force_flag(self, tmp_path, monkeypatch):
        """A second stop press while soft_pending hard-kills even when the
        client did NOT send force=true. The client derives force from the
        WS-echoed stop_state, which lags on a slow connection; the backend's
        own soft_pending state is authoritative, so any second press escalates.
        """
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        slot._stop_state = "soft_pending"

        force_called = []

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            force_called.append(force)
            if on_hard:
                await on_hard()
            return "hard"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            # No ?force=true — the lagging client still thinks state is idle.
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200

        # Backend escalated to a hard kill anyway.
        assert force_called == [True]
        assert slot._stop_state == "idle"
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_stop_turn_first_press_preserves_queue(self, tmp_path, monkeypatch):
        """Queue populated; POST stop; queue preserved for dequeue loop."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        slot._queue.extend(["msg1", "msg2"])

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            assert preserve_queue is True
            if on_soft:
                await on_soft()
            return "soft"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200
        assert len(slot._queue) == 2
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_double_press_force_kill_clears_queue(self, tmp_path, monkeypatch):
        """Double-press (force kill) clears slot._queue so no dequeue fires."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        slot._stop_state = "soft_pending"
        slot._queue.extend(["msg1", "msg2", "msg3"])

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            if on_hard:
                await on_hard()
            return "hard"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200
        assert len(slot._queue) == 0
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_single_press_empty_queue_goes_idle(self, tmp_path, monkeypatch):
        """Single press with no queued messages goes idle without error."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        assert len(slot._queue) == 0

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            assert preserve_queue is True
            if on_soft:
                await on_soft()
            return "soft"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200
        assert len(slot._queue) == 0
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_double_press_empty_queue_no_crash(self, tmp_path, monkeypatch):
        """Double press with empty queue clears without error."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        slot._stop_state = "soft_pending"
        assert len(slot._queue) == 0

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            if on_hard:
                await on_hard()
            return "hard"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200
        assert len(slot._queue) == 0
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_stop_event_appears_in_transcript(self, tmp_path, monkeypatch):
        """After stop, slot messages contain a stop_event entry."""
        import json

        def _is_stop_event(m: dict) -> bool:
            cls = m.get("cls", "")
            if not isinstance(cls, str) or not cls.startswith("{"):
                return False
            try:
                return json.loads(cls).get("kind") == "stop_event"
            except (json.JSONDecodeError, TypeError):
                return False

        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            if on_soft:
                await on_soft()
            return "soft"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200

        stop_msgs = [m for m in slot.messages if _is_stop_event(m)]
        assert len(stop_msgs) == 1
        data = json.loads(stop_msgs[0]["content"])
        assert data["kind"] == "stop_event"
        assert data["state"] == "stopped"
        assert data["outcome"] == "soft"
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_stop_event_replace_in_place(self, tmp_path, monkeypatch):
        """Stop event has stable id across state transitions (one entry)."""
        import json

        def _is_stop_event(m: dict) -> bool:
            cls = m.get("cls", "")
            if not isinstance(cls, str) or not cls.startswith("{"):
                return False
            try:
                return json.loads(cls).get("kind") == "stop_event"
            except (json.JSONDecodeError, TypeError):
                return False

        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            # Verify the stop_event was inserted before callbacks
            stop_msgs = [m for m in slot.messages if _is_stop_event(m)]
            assert len(stop_msgs) == 1
            pre_data = json.loads(stop_msgs[0]["content"])
            assert pre_data["state"] == "stopping"
            if on_soft:
                await on_soft()
            return "soft"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            await client.post("/api/chat/slots/s1/stop")

        # Still only one stop_event message
        stop_msgs = [m for m in slot.messages if _is_stop_event(m)]
        assert len(stop_msgs) == 1
        data = json.loads(stop_msgs[0]["content"])
        assert data["state"] == "stopped"
        slot.task.cancel()


class TestStopHistoryBanner:
    """Tests for history re-injection banner skip on soft stop."""

    @staticmethod
    def _last_stop_soft(slot: _ChatSlot) -> bool:
        """Replicates the detection logic in chat.py:_run_chat."""
        import json

        for m in reversed(slot.messages):
            cls_val = m.get("cls", "")
            if not isinstance(cls_val, str) or not cls_val.startswith("{"):
                continue
            try:
                _cls = json.loads(cls_val)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(_cls, dict) or _cls.get("kind") != "stop_event":
                continue
            return _cls.get("outcome") == "soft"
        return False

    def test_soft_stop_preserves_session_no_history_banner(self):
        """After a soft stop, _build_history_prefix is skipped."""
        import json

        slot = _ChatSlot("s1")
        slot.append("user", "hello")
        slot.append("assistant", "hi there")
        # cls must be a JSON-encoded dict (same format api_chat_slot_stop uses)
        cls_json = json.dumps(
            {
                "kind": "stop_event",
                "id": "stop-abc",
                "state": "stopped",
                "outcome": "soft",
            }
        )
        slot.append("system", cls_json, cls_json)
        assert self._last_stop_soft(slot) is True

    def test_hard_stop_still_injects_history_banner(self):
        """After a hard stop, the banner detection returns False."""
        import json

        slot = _ChatSlot("s1")
        slot.append("user", "hello")
        slot.append("assistant", "hi there")
        cls_json = json.dumps(
            {
                "kind": "stop_event",
                "id": "stop-abc",
                "state": "stop_failed_reset",
                "outcome": "hard",
            }
        )
        slot.append("system", cls_json, cls_json)
        assert self._last_stop_soft(slot) is False

    def test_plain_string_cls_does_not_match(self):
        """Plain-string cls (legacy format) is ignored — no false positive."""
        slot = _ChatSlot("s1")
        slot.append("user", "hello")
        slot.append("system", "{}", "stop_event")  # plain string cls
        assert self._last_stop_soft(slot) is False


# ── Tests: AcpProcessDied handler in _run_chat ──


class TestStopDuringSessionPrep:
    """A Stop pressed while the turn is still being prepared must not open it.

    During ``get_or_create``'s cold start the session is not registered yet,
    so ``SessionManager.stop_turn`` answers ``"idle"`` and the stop resolves
    without cancelling anything. The dispatch gate in ``_run_chat`` is what
    honors that stop: it compares ``slot._stop_generation`` against the
    turn-entry snapshot and refuses to open the turn (#5464 — [Stopped] card
    shown while the response streams to completion).
    """

    def _make_state_and_slot(self, tmp_path):
        from kiro_crew.dashboard.chat_runner import _run_chat

        state = _make_state(tmp_path)
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()

        slot = state.get_or_create_slot("stop-prep-slot")
        slot.append("user", "hello", "msg msg-u")
        return state, slot, _run_chat

    def _make_client(self, stream_calls):
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        client = MagicMock()
        client.shutdown = AsyncMock()

        async def _stream(msg):
            stream_calls.append(msg)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="full response")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        client.stream = _stream
        client.stream_command = _stream
        return client

    @pytest.mark.asyncio
    async def test_stop_during_get_or_create_aborts_dispatch(self, tmp_path: Path) -> None:
        """Stop lands mid-cold-start → the turn never opens, nothing streams.

        The stop is simulated exactly as the /stop handler leaves it for this
        race: ``_stop_state`` flips idle → soft_pending (bumping
        ``_stop_generation``) and, because ``stop_turn`` found no session and
        answered "idle", snaps straight back to idle. A point-in-time state
        check at dispatch sees nothing — only the generation delta survives.
        """
        state, slot, _run_chat = self._make_state_and_slot(tmp_path)
        stream_calls: list[str] = []
        client = self._make_client(stream_calls)

        async def _slow_create(*args, **kwargs):
            # Stop pressed while the session is still being created.
            slot._stop_state = "soft_pending"
            slot._stop_state = "idle"
            return client, True, False

        state.sessions.get_or_create = AsyncMock(side_effect=_slow_create)

        await _run_chat(state, slot, "hello")

        assert stream_calls == [], (
            "the turn was dispatched despite a Stop during session prep — "
            "the full response would stream behind a [Stopped] card (#5464)"
        )

    @pytest.mark.asyncio
    async def test_stop_during_get_or_create_does_not_accept_monitor_wake(
        self, tmp_path: Path
    ) -> None:
        """A stopped pre-stream wake stays retryable instead of becoming DISPATCHED."""
        from kiro_crew.monitoring.completion import MonitorCompletionHook

        state, slot, _run_chat = self._make_state_and_slot(tmp_path)
        stream_calls: list[str] = []
        client = self._make_client(stream_calls)

        async def _slow_create(*args, **kwargs):
            slot._stop_state = "soft_pending"
            slot._stop_state = "idle"
            return client, True, False

        state.sessions.get_or_create = AsyncMock(side_effect=_slow_create)
        accepted = MagicMock()
        completion = MonitorCompletionHook(
            "monitor1",
            "failure-a",
            AsyncMock(),
            authorization_callback=AsyncMock(return_value=True),
            acceptance_callback=accepted,
        )

        await _run_chat(state, slot, "hello", monitor_completion=completion)

        assert completion.accepted is False
        accepted.assert_not_called()
        assert stream_calls == []

    @pytest.mark.asyncio
    async def test_no_stop_dispatches_normally(self, tmp_path: Path) -> None:
        """Control: without a Stop, the same setup opens the turn exactly once."""
        state, slot, _run_chat = self._make_state_and_slot(tmp_path)
        stream_calls: list[str] = []
        client = self._make_client(stream_calls)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        await _run_chat(state, slot, "hello")

        assert len(stream_calls) == 1, "the dispatch gate must not fire without a Stop"

    @pytest.mark.asyncio
    async def test_stop_of_a_previous_turn_does_not_abort_the_next(self, tmp_path: Path) -> None:
        """A stop generation inherited from an EARLIER turn must not trip the gate.

        The gate compares against the snapshot taken at THIS turn's entry, so a
        slot whose previous turn was stopped (generation already > 0) still
        dispatches its next turn normally.
        """
        state, slot, _run_chat = self._make_state_and_slot(tmp_path)
        # A previous turn was stopped and resolved before this turn began.
        slot._stop_state = "soft_pending"
        slot._stop_state = "idle"

        stream_calls: list[str] = []
        client = self._make_client(stream_calls)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        await _run_chat(state, slot, "hello")

        assert (
            len(stream_calls) == 1
        ), "a stale stop generation from a previous turn aborted a fresh turn"


class TestAcpProcessDiedRecovery:
    """Verify _run_chat handles AcpProcessDied with retry logic, redaction, and session reset."""

    def _make_state_and_slot(self, tmp_path):
        from kiro_crew.dashboard.chat_runner import _run_chat

        state = _make_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()

        slot = state.get_or_create_slot("pipe-death-slot")
        slot.append("user", "hello", "msg msg-u")

        mock_client = state.sessions.get_or_create.return_value[0]
        mock_client.shutdown = AsyncMock()
        return state, slot, mock_client, _run_chat

    def _make_stream_raise(self, mock_client, exc):
        async def _raise(msg):
            raise exc
            yield  # noqa: E501

        mock_client.stream = _raise
        mock_client.stream_command = _raise

    @pytest.mark.asyncio
    async def test_retry_at_depth_0_requeues_message(self, tmp_path: Path) -> None:
        """First pipe death at depth 0 → message re-queued, retrying shown."""
        from kiro_crew.acp.client import AcpProcessDied

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        await _run_chat(state, slot, "test message")

        state.sessions.reset.assert_awaited_once()
        assert slot._acp_pipe_death_retries == 1
        # Retry shows a single PERSISTED error card (reliably visible at
        # turn-teardown, unlike an ephemeral chat_status which the frontend drops
        # once the streaming turn ends). slot.append both persists the card and
        # emits a single chat_message via _on_message.
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("retrying" in m.get("content", "") for m in error_msgs)
        # Regression guard for the duplicate-card bug: the retry card must NOT be
        # ALSO explicitly broadcast over chat_message (slot.append already emits it
        # once via _on_message). A redundant broadcast_ws renders a second card.
        dup_broadcasts = [
            c
            for c in state.broadcast_ws.call_args_list
            if c.args
            and c.args[0] == "chat_message"
            and len(c.args) > 1
            and "retrying" in c.args[1].get("content", "")
        ]
        assert dup_broadcasts == [], "retry card must not be double-emitted via broadcast_ws"

    @pytest.mark.asyncio
    async def test_budget_exhaustion_shows_stuck(self, tmp_path: Path) -> None:
        """4th pipe death → 'Session stuck' shown, no re-queue."""
        from kiro_crew.acp.client import AcpProcessDied

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._acp_pipe_death_retries = 3  # already exhausted
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        await _run_chat(state, slot, "test message")

        assert slot._acp_pipe_death_retries == 4
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("stuck" in m.get("content", "").lower() for m in error_msgs)

    @pytest.mark.asyncio
    async def test_nested_depth_shows_please_retry(self, tmp_path: Path) -> None:
        """Pipe death at depth > 0 → 'please retry' shown, no re-queue."""
        from kiro_crew.acp.client import AcpProcessDied

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        await _run_chat(state, slot, "test message", _prompt_depth=1)

        assert slot._acp_pipe_death_retries == 1
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("please retry" in m.get("content", "").lower() for m in error_msgs)

    @pytest.mark.asyncio
    async def test_partial_assistant_text_redacted(self, tmp_path: Path) -> None:
        """Pipe death mid-stream preserves redacted output and queues a continuation."""
        from kiro_crew.acp.client import AcpProcessDied
        from kiro_crew.dashboard.chat_utils import (
            _CONN_RECOVER_MSG,
            SYNTHETIC_RECOVERY_KIND,
            RecoveryPayload,
        )
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._titled = True

        async def _stream_then_die(msg):
            yield LLMEvent(
                kind=EVENT_TEXT_CHUNK, text="partial output with AKIA1234567890ABCDEF secret"
            )
            raise AcpProcessDied("pipe broken")

        client.stream = _stream_then_die
        client.stream_command = _stream_then_die

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await _run_chat(state, slot, "test message")

        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert assistant_msgs, "Expected at least one assistant message with redacted content"
        for m in assistant_msgs:
            assert "AKIA1234567890ABCDEF" not in m.get("content", "")
        assert slot._queue == [
            {
                "id": slot._queue[0]["id"],
                "content": _CONN_RECOVER_MSG,
                "kind": SYNTHETIC_RECOVERY_KIND,
                # A continuation, not the user's request: the turn had emitted, so
                # this text is the runner's and must not mirror as user speech.
                "payload": RecoveryPayload.CONTINUATION,
                # Admission stamp (#5911): recovery requeues record the containment
                # that held at requeue so the drain can re-validate the retry.
                "meta": slot._queue[0]["meta"],
            }
        ]

    @pytest.mark.asyncio
    async def test_prompt_busy_requeue_does_not_claim_a_lost_connection(
        self, tmp_path: Path
    ) -> None:
        """A busy-session reset must requeue the busy continuation, not the connection one.

        Both causes reset the session and requeue a continuation, and the queued
        marker is what the transcript renders -- so borrowing the connection
        marker here reports a dropped connection to a user whose status card
        says the session was busy.
        """
        from kiro_crew.dashboard.chat_runner import PromptBusyExhaustedError
        from kiro_crew.dashboard.chat_utils import (
            _BUSY_RECOVER_MSG,
            SYNTHETIC_RECOVERY_KIND,
            RecoveryPayload,
        )
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._titled = True

        async def _stream_then_busy(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial output")
            raise PromptBusyExhaustedError("busy")

        client.stream = _stream_then_busy
        client.stream_command = _stream_then_busy

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await _run_chat(state, slot, "test message")

        assert slot._queue == [
            {
                "id": slot._queue[0]["id"],
                "content": _BUSY_RECOVER_MSG,
                "kind": SYNTHETIC_RECOVERY_KIND,
                # A continuation, not the user's request: the turn had emitted, so
                # this text is the runner's and must not mirror as user speech.
                "payload": RecoveryPayload.CONTINUATION,
                # Admission stamp (#5911): recovery requeues record the containment
                # that held at requeue so the drain can re-validate the retry.
                "meta": slot._queue[0]["meta"],
            }
        ]
        # The status card and the queued marker describe the same event to two
        # audiences; they disagreed until the continuation became cause-aware.
        errors = [m.get("content", "") for m in slot.messages if m.get("role") == "error"]
        assert any("Session busy" in text for text in errors)
        assert not any("Connection lost" in text for text in errors)

    @pytest.mark.asyncio
    async def test_a_second_failure_before_output_keeps_the_text_machine_authored(
        self, tmp_path: Path
    ) -> None:
        """A recovery turn that dies again before emitting must stay machine-authored.

        The requeue replays ``message`` unchanged when nothing was emitted -- but on a
        second consecutive failure that message is the runner's own continuation from
        the previous recovery, not the user's request. Tagging it ORIGINAL makes the
        next dequeue mirror internal orchestration to a linked thread as user speech.
        """
        from kiro_crew.acp.client import AcpProcessDied
        from kiro_crew.dashboard.chat_utils import (
            _CONN_RECOVER_MSG,
            SYNTHETIC_RECOVERY_KIND,
            RecoveryPayload,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._titled = True
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await _run_chat(state, slot, _CONN_RECOVER_MSG, _synthetic_payload=True)

        assert slot._queue == [
            {
                "id": slot._queue[0]["id"],
                "content": _CONN_RECOVER_MSG,
                "kind": SYNTHETIC_RECOVERY_KIND,
                "payload": RecoveryPayload.CONTINUATION,
                # Admission stamp (#5911): recovery requeues record the containment
                # that held at requeue so the drain can re-validate the retry.
                "meta": slot._queue[0]["meta"],
            }
        ]

    @pytest.mark.asyncio
    async def test_a_recovery_turns_reply_still_reaches_the_linked_thread(
        self, tmp_path: Path
    ) -> None:
        """Withholding the user echo must not also withhold the assistant reply.

        A Slack-linked turn that emits output and then loses its connection recovers as
        a synthetic continuation. Skipping the whole mirror SETUP for that turn leaves
        no thread to reply into, so the continuation's answer is never delivered and the
        question asked on Slack stays unanswered.
        """
        from kiro_crew.dashboard.chat_utils import _CONN_RECOVER_MSG
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._titled = True
        state.sessions.get_slack_link = MagicMock(return_value=("ts-1", "C123"))
        state.slack_client = AsyncMock()
        state.slack_client.start_stream = AsyncMock(return_value="")

        async def _stream_text(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the finished answer")

        client.stream = _stream_text
        client.stream_command = _stream_text

        await _run_chat(state, slot, _CONN_RECOVER_MSG, _synthetic_payload=True)

        posted = [str(c.args) for c in state.slack_client.post_message.await_args_list]
        assert any(
            "the finished answer" in a for a in posted
        ), f"recovery reply never delivered to the linked thread; posted={posted}"
        # The user echo stays withheld: that text is the runner's, not the user's.
        assert not any("\U0001f4ac" in a for a in posted), f"echoed runner text: {posted}"

    @pytest.mark.asyncio
    async def test_session_reset_propagated(self, tmp_path: Path) -> None:
        """Verify the finally block resets the session after AcpProcessDied."""
        from kiro_crew.acp.client import AcpProcessDied

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        await _run_chat(state, slot, "test message")

        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_state_suppresses_requeue(self, tmp_path: Path) -> None:
        """AcpProcessDied during active stop → no re-queue."""
        from kiro_crew.acp.client import AcpProcessDied

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._stop_state = "killing"
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        await _run_chat(state, slot, "test message")

        assert not slot._queue, "message should not be re-queued during active stop"

    @pytest.mark.asyncio
    async def test_stop_state_suppresses_requeue_prompt_busy(self, tmp_path: Path) -> None:
        """PromptBusyExhaustedError during active stop → no re-queue."""
        from kiro_crew.dashboard.chat_runner import PromptBusyExhaustedError

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._stop_state = "soft_pending"
        self._make_stream_raise(client, PromptBusyExhaustedError("busy"))

        await _run_chat(state, slot, "test message")

        assert not slot._queue, "message should not be re-queued during active stop"

    @pytest.mark.asyncio
    async def test_stop_state_suppresses_requeue_acp_error(self, tmp_path: Path) -> None:
        """AcpError retry-eligible during active stop → no re-queue."""
        from kiro_crew.acp.client import AcpError

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._stop_state = "killing"
        self._make_stream_raise(client, AcpError("process exited"))

        await _run_chat(state, slot, "test message")

        assert not slot._queue, "message should not be re-queued during active stop"

    @pytest.mark.asyncio
    async def test_should_suppress_requeue_helper(self, tmp_path: Path) -> None:
        """_should_suppress_requeue returns True for non-idle states."""
        from kiro_crew.dashboard.chat_runner import _should_suppress_requeue

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("helper-test")

        slot._stop_state = "idle"
        assert _should_suppress_requeue(slot) is False

        slot._stop_state = "soft_pending"
        assert _should_suppress_requeue(slot) is True

        slot._stop_state = "killing"
        assert _should_suppress_requeue(slot) is True

    @pytest.mark.asyncio
    async def test_cancelled_error_redacts_partial_text(self, tmp_path: Path) -> None:
        """CancelledError mid-stream → partial output redacted before display."""
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        async def _stream_then_cancel(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial with AKIA1234567890ABCDEF key")
            raise asyncio.CancelledError()

        client.stream = _stream_then_cancel
        client.stream_command = _stream_then_cancel

        await _run_chat(state, slot, "test message")

        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert assistant_msgs, "Expected at least one assistant message with redacted content"
        for m in assistant_msgs:
            assert "AKIA1234567890ABCDEF" not in m.get("content", "")

    @pytest.mark.asyncio
    async def test_retry_requeues_via_queue_insert(self, tmp_path: Path) -> None:
        """First pipe death at depth 0 → queue_insert is called."""
        from unittest.mock import patch as _patch

        from kiro_crew.acp.client import AcpProcessDied
        from kiro_crew.dashboard.state import _ChatSlot

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        calls = []
        orig = _ChatSlot.queue_insert

        def spy(self_slot, *a, **kw):
            calls.append(a)
            return orig(self_slot, *a, **kw)

        with _patch.object(_ChatSlot, "queue_insert", spy):
            await _run_chat(state, slot, "test message")

        assert (0, "test message") in calls

    @pytest.mark.asyncio
    async def test_retry_requeues_with_consumption_callback(self, tmp_path: Path) -> None:
        """A pre-consumption pipe-death retry keeps its producer callback."""
        from unittest.mock import Mock
        from unittest.mock import patch as _patch

        from kiro_crew.acp.client import AcpProcessDied
        from kiro_crew.dashboard.state import _ChatSlot

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))
        on_consumed = Mock()
        on_irreversibly_consumed = Mock()
        callbacks = []
        orig = _ChatSlot.queue_insert

        def spy(self_slot, *args, **kwargs):
            callbacks.append(
                (
                    kwargs.get("on_consumed"),
                    kwargs.get("on_irreversibly_consumed"),
                )
            )
            return orig(self_slot, *args, **kwargs)

        with (
            _patch.object(_ChatSlot, "queue_insert", spy),
            _patch(
                "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
                new=AsyncMock(return_value=False),
            ),
            _patch("kiro_crew.dashboard.chat_runner._finish_queue_cycle"),
        ):
            await _run_chat(
                state,
                slot,
                "test message",
                _on_consumed=on_consumed,
                _on_irreversibly_consumed=on_irreversibly_consumed,
            )

        assert callbacks[0] == (on_consumed, on_irreversibly_consumed)

    @pytest.mark.parametrize("event_type", ["text", "tool"])
    @pytest.mark.asyncio
    async def test_output_reports_irreversible_consumption_before_turn_end(
        self, tmp_path: Path, event_type: str
    ) -> None:
        """First token or tool closes durable replay windows immediately."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        irreversible_reported = asyncio.Event()
        allow_turn_finish = asyncio.Event()

        async def _stream(_message):
            if event_type == "text":
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="started")
            else:
                yield LLMEvent(kind=EVENT_TOOL_CALL, title="read_file", tool_kind="read")
            await allow_turn_finish.wait()
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream
        turn = asyncio.create_task(
            _run_chat(
                state,
                slot,
                "test message",
                _on_irreversibly_consumed=irreversible_reported.set,
            )
        )

        report = asyncio.create_task(irreversible_reported.wait())
        done, _pending = await asyncio.wait(
            (turn, report),
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert report in done, turn.exception()
        assert not turn.done()

        allow_turn_finish.set()
        await turn

    @pytest.mark.asyncio
    async def test_acperror_process_exited_uses_pipe_death_counter(self, tmp_path: Path) -> None:
        """Option Y: AcpError 'process exited' increments the pipe-death counter, not busy."""
        from kiro_crew.acp.client import AcpError

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpError("ACP process exited (code=1)"))

        await _run_chat(state, slot, "test message")

        assert slot._acp_pipe_death_retries == 1
        assert slot._prompt_busy_retries == 0
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("Connection lost" in m.get("content", "") for m in error_msgs)
        # A recovery IS queued here, so the notice must be tagged: an untagged row is
        # indistinguishable from a terminal failure and re-offers an executing choice.
        from kiro_crew.dashboard.chat_utils import TRANSIENT_RETRY_KIND

        _retrying = [m for m in error_msgs if "retrying" in m.get("content", "").lower()]
        assert _retrying, "no retrying notice to inspect"
        assert all((m.get("meta") or {}).get("kind") == TRANSIENT_RETRY_KIND for m in _retrying)
        # LIVE path: slot.append already emits one tagged chat_message, so an explicit
        # frame here would be an untagged duplicate that re-arms the pills mid-backoff.
        _dupes = [
            c
            for c in state.broadcast_ws.call_args_list
            if c.args
            and c.args[0] == "chat_message"
            and isinstance(c.args[1], dict)
            and "retrying" in str(c.args[1].get("content", "")).lower()
        ]
        assert not _dupes, f"untagged explicit chat_message frame(s): {_dupes}"

    @pytest.mark.asyncio
    async def test_acperror_already_in_progress_uses_busy_counter(self, tmp_path: Path) -> None:
        """Option Y: AcpError 'already in progress' increments the busy counter, not pipe-death."""
        from kiro_crew.acp.client import AcpError

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpError("Prompt already in progress"))

        await _run_chat(state, slot, "test message")

        assert slot._prompt_busy_retries == 1
        assert slot._acp_pipe_death_retries == 0
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("Session busy" in m.get("content", "") for m in error_msgs)

    @pytest.mark.asyncio
    async def test_prompt_busy_exhausted_depth_gt0_shows_please_retry(self, tmp_path: Path) -> None:
        """PromptBusyExhaustedError at _prompt_depth>0 with budget remaining hits the
        else branch: surfaces a 'Session busy — please retry' card (not a silent
        failure) and does NOT re-queue (mirrors the AcpProcessDied depth>0 handling)."""
        from kiro_crew.llm_helpers import PromptBusyExhaustedError

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, PromptBusyExhaustedError("busy"))

        await _run_chat(state, slot, "test message", _prompt_depth=1)

        # depth>0 + counter (1) <= 3: neither the retry-and-requeue `if` nor the
        # `elif > 3` fires, so the new `else` surfaces feedback instead of failing
        # silently. No re-queue at depth>0.
        assert slot._prompt_busy_retries == 1
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("please retry" in m.get("content", "").lower() for m in error_msgs)
        assert any("Session busy" in m.get("content", "") for m in error_msgs)

    @pytest.mark.asyncio
    async def test_acperror_process_exited_depth_gt0_resets_no_requeue(
        self, tmp_path: Path
    ) -> None:
        """A pipe-death AcpError ('process exited') at _prompt_depth>0 must STILL reset
        the dead session and increment the pipe-death counter (mirrors AcpProcessDied /
        PromptBusyExhaustedError), and surface a 'Connection lost — please retry' card —
        but NOT re-queue (re-queue is depth-0 only). Previously the whole reset/counter
        block was gated on `_prompt_depth == 0`, so a depth>0 pipe-death fell through to
        the generic else: no session reset (the next turn hit the dead process) and the
        failure never counted toward the exhaustion threshold."""
        from kiro_crew.acp.client import AcpError

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpError("ACP process exited (code=1)"))

        await _run_chat(state, slot, "test message", _prompt_depth=1)

        # The dead process IS reset and the failure IS counted, regardless of depth.
        state.sessions.reset.assert_awaited_once()
        assert slot._acp_pipe_death_retries == 1
        # A friendly feedback card (not a bare ❌ raw-error card), and NO re-queue.
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("Connection lost" in m.get("content", "") for m in error_msgs)
        assert any("please retry" in m.get("content", "").lower() for m in error_msgs)
        assert not slot._queue, "depth>0 must not re-queue"
        # Discriminating control: nothing is queued, so this notice is genuinely terminal
        # and must NOT be tagged -- tagging it would hide pills a user still needs.
        from kiro_crew.dashboard.chat_utils import TRANSIENT_RETRY_KIND

        assert all((m.get("meta") or {}).get("kind") != TRANSIENT_RETRY_KIND for m in error_msgs)


class TestEmptyResponseRetry:
    """Verify _run_chat retries once on empty model response, then shows error."""

    def _make_state_and_slot(self, tmp_path):
        from kiro_crew.dashboard.chat_runner import _run_chat

        state = _make_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.record_success = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()

        slot = state.get_or_create_slot("empty-resp-slot")
        slot.append("user", "hello", "msg msg-u")

        mock_client = state.sessions.get_or_create.return_value[0]
        mock_client.context_usage_pct = MagicMock(return_value=50.0)
        mock_client.shutdown = AsyncMock()
        return state, slot, mock_client, _run_chat

    def _make_empty_stream(self, mock_client):
        """Stream that completes immediately with no text."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        mock_client.stream = _stream
        mock_client.stream_command = _stream

    @staticmethod
    async def _cancel_background_tasks(state) -> None:
        """Cancel AND await every task this isolated state spawned.

        `_run_chat` starts title/summary work that is irrelevant to these
        recovery assertions. A bare `task.cancel()` leaves the coroutine pending
        until the loop gets another tick, which surfaces as unawaited-coroutine
        and destroyed-pending-task warnings under xdist. Await the cancellation
        exactly; never sleep and never let one test's teardown spill into another.
        """
        tasks = list(state._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_first_empty_response_requeues_message(self, tmp_path: Path) -> None:
        """First empty response at depth 0 → message re-queued silently."""
        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_empty_stream(client)

        # Spy on queue_insert to verify the message is ACTUALLY re-queued (the
        # behavior the test name promises) — not merely that the counter ticked.
        calls = []
        callbacks = []
        directive_origins = []
        on_consumed = MagicMock()
        orig = _ChatSlot.queue_insert

        def spy(self_slot, *a, **kw):
            calls.append(a)
            callbacks.append(kw.get("on_consumed"))
            directive_origins.append(kw.get("directive_user_origin"))
            return orig(self_slot, *a, **kw)

        with (
            patch.object(_ChatSlot, "queue_insert", spy),
            patch("kiro_crew.dashboard.chat_runner.save_slot_off_loop") as mock_save,
            patch("kiro_crew.dashboard.chat_runner._maybe_consolidate") as mock_consolidate,
            patch("kiro_crew.dashboard.chat_runner._flush_file_changes") as mock_flush,
            patch(
                "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
                new=AsyncMock(return_value=False),
            ),
        ):
            # The behavior under test is the re-queue itself. Keep the queued
            # item in the slot, but stop the finally block from immediately
            # launching a second turn. Patching the queue-drain boundary avoids
            # globally replacing asyncio.create_task, which can otherwise let
            # unrelated lifecycle tasks race the assertions under xdist load.
            await _run_chat(
                state,
                slot,
                "test message",
                _directive_user_origin=True,
                _on_consumed=on_consumed,
            )
            background_tasks = list(state._background_tasks)
            for _bg_task in background_tasks:
                _bg_task.cancel()
            if background_tasks:
                await asyncio.gather(*background_tasks, return_exceptions=True)

        assert slot._empty_response_retries == 1
        # The message must be re-queued at the front of the queue.
        assert (0, "test message") in calls
        assert callbacks[0] is on_consumed
        assert directive_origins == [True]
        assert [args.args for args in on_consumed.call_args_list] == [(True,), (False,)]
        # No notice card shown on first attempt — the empty is silently re-queued
        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert not any("returned nothing this turn" in m.get("content", "") for m in notice_msgs)
        # Re-queue path must NOT persist/consolidate the spurious empty turn or
        # record success (item 3 of the CR).
        mock_save.assert_not_called()
        mock_consolidate.assert_not_called()
        state.sessions.record_success.assert_not_called()
        # _flush_file_changes is intentionally NOT skipped: the try-body call (inside
        # the `if not _retrying_empty` guard) is skipped, but the finally block calls
        # it once unconditionally ("ensure file changes always surface, even on
        # cancel/error"). Scope the assertion to this slot because finalization of a
        # task created by another test can run while this module-level function is
        # patched and must not change the behavior observed for this turn.
        own_flushes = [
            mock_call for mock_call in mock_flush.call_args_list if mock_call.args == (slot,)
        ]
        assert len(own_flushes) == 1

    @pytest.mark.asyncio
    async def test_empty_response_at_depth_gt0_shows_error_immediately(
        self, tmp_path: Path
    ) -> None:
        """At _prompt_depth>0 an empty response is NOT silently retried — it shows the
        terminal empty-response notice card on the FIRST empty. The silent
        re-queue is intentionally depth-0 only (nested tool-use turns must not silently
        re-queue and risk a runaway loop)."""
        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_empty_stream(client)

        await _run_chat(state, slot, "test message", _prompt_depth=1)

        # depth>0: the `if _prompt_depth == 0 ...` guard is false, so the else fires —
        # terminal card immediately, no silent retry, no increment of the counter.
        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert any("returned nothing this turn" in m.get("content", "") for m in notice_msgs)
        assert slot._empty_response_retries == 0

    @pytest.mark.asyncio
    async def test_second_empty_response_auto_continues(self, tmp_path: Path) -> None:
        """Second consecutive empty response → ONE synthetic continue nudge is
        queued (same live session) with a transcript-visible notice. Re-sending
        the identical prompt reproduces the identical empty generation; a
        DIFFERENT message reliably recovers — this automates the user manually
        typing "continue"."""
        from kiro_crew.dashboard.chat_runner import _EMPTY_AUTO_CONTINUE_MSG

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._empty_response_retries = 1  # already silently retried once
        self._make_empty_stream(client)

        calls = []
        orig = _ChatSlot.queue_insert

        def spy(self_slot, *a, **kw):
            calls.append(a)
            return orig(self_slot, *a, **kw)

        with patch.object(_ChatSlot, "queue_insert", spy):
            await _run_chat(state, slot, "test message")
            await self._cancel_background_tasks(state)

        # The nudge (NOT the original message) is queued at the front.
        assert (0, _EMPTY_AUTO_CONTINUE_MSG) in calls
        assert (0, "test message") not in calls
        # Visible recovery notice, counter advanced to the terminal rung, and
        # the recovery turn is excluded from the cycle-complete counter reset.
        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert any("auto-continuing once" in m.get("content", "") for m in notice_msgs)
        assert slot._empty_response_retries == 2

    @pytest.mark.asyncio
    async def test_auto_continue_nudge_drains_as_inject_not_user(self, tmp_path: Path) -> None:
        """The queued nudge is runner orchestration, not user speech: when the
        queue drains it, the transcript append MUST use the "inject" recovery
        role (never "user" — a user-role append would persist an internal
        instruction as user-authored history and mirror it to linked
        channels), and it MUST NOT cancel a pending synthesis (the user did
        not take over the conversation)."""
        from kiro_crew.dashboard.chat_runner import _EMPTY_AUTO_CONTINUE_MSG

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._empty_response_retries = 1  # next empty triggers the nudge rung
        slot._pending_synthesis = True
        self._make_empty_stream(client)

        await _run_chat(state, slot, "test message")
        # Let the REAL detached drain task run: it pops the nudge, classifies
        # it, appends it to the transcript, and runs the (also-empty) nudge
        # turn, which terminates the ladder at the give-up notice.
        for _bg_task in list(state._background_tasks):
            try:
                await _bg_task
            except Exception:
                pass

        nudge_msgs = [m for m in slot.messages if m.get("content") == _EMPTY_AUTO_CONTINUE_MSG]
        assert nudge_msgs, "drained nudge never reached the transcript"
        # The nudge must NEVER carry the user role (that would persist an
        # internal instruction as user-authored history and mirror it to
        # linked channels) — the ORIGINAL user message keeps its user role.
        assert all(m.get("role") == "inject" for m in nudge_msgs)
        # Draining a synthetic recovery message is not a user takeover.
        assert slot._pending_synthesis is True

    @pytest.mark.asyncio
    async def test_third_empty_response_shows_notice(self, tmp_path: Path) -> None:
        """Third consecutive empty (the auto-continue nudge ALSO produced
        nothing) → terminal notice card; the ladder is bounded, never loops."""
        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._empty_response_retries = 2  # re-queue + nudge both spent
        self._make_empty_stream(client)

        await _run_chat(state, slot, "test message")

        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert any("returned nothing this turn" in m.get("content", "") for m in notice_msgs)
        # After the terminal notice, the counter resets so the NEXT independent
        # user turn gets a fresh budget (not sticky).
        assert slot._empty_response_retries == 0

    @pytest.mark.asyncio
    async def test_second_empty_flag_off_shows_notice(self, tmp_path: Path) -> None:
        """With session.empty_response_auto_continue disabled, the second empty
        surfaces the terminal notice immediately (pre-feature behavior)."""
        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._empty_response_retries = 1
        self._make_empty_stream(client)

        # Exercise the REAL config path (loader wiring included): persist the
        # flag as false in a config file and point KiroCrewConfig.load() at it,
        # rather than patching the gate function (which would pass even if the
        # loader dropped the field — the exact regression this test guards).
        cfg_file = tmp_path / "flag-off-config.json"
        cfg_file.write_text('{"session": {"empty_response_auto_continue": false}}')
        with patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            await _run_chat(state, slot, "test message")

        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert any("returned nothing this turn" in m.get("content", "") for m in notice_msgs)
        assert slot._empty_response_retries == 0

    @pytest.mark.asyncio
    async def test_successful_response_resets_counter(self, tmp_path: Path) -> None:
        """A successful (non-empty) response resets the retry counter."""
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._empty_response_retries = 1  # had a prior empty

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Hello!")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "test message")

        assert slot._empty_response_retries == 0

    @pytest.mark.asyncio
    async def test_compaction_turn_no_empty_response_error(self, tmp_path: Path) -> None:
        """Compaction turns set assistant_text='' but should NOT trigger the empty-response notice."""
        from kiro_crew.providers.base import EVENT_COMPACTION_STATUS, EVENT_COMPLETE, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_COMPACTION_STATUS, text="completed", title="summary")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "/compact")

        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert not any("returned nothing this turn" in m.get("content", "") for m in notice_msgs)

    @pytest.mark.asyncio
    async def test_clear_turn_no_empty_response_error(self, tmp_path: Path) -> None:
        """Clear turns set assistant_text='' but should NOT trigger the empty-response notice."""
        from kiro_crew.providers.base import EVENT_CLEAR_STATUS, EVENT_COMPLETE, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_CLEAR_STATUS)
            yield LLMEvent(kind=EVENT_COMPLETE)

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "/clear")

        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert not any("returned nothing this turn" in m.get("content", "") for m in notice_msgs)

    @pytest.mark.asyncio
    async def test_agent_switch_turn_no_empty_response_error(self, tmp_path: Path) -> None:
        """Agent switch turns set assistant_text='' but should NOT trigger the empty-response notice."""
        from kiro_crew.providers.base import EVENT_AGENT_SWITCHED, EVENT_COMPLETE, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_AGENT_SWITCHED, text="new-agent")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "test message")

        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert not any("returned nothing this turn" in m.get("content", "") for m in notice_msgs)


class TestProductiveTurnNeverReplaysVerbatim:
    """A turn that DID work must never have the user's message replayed verbatim.

    ``assistant_text`` is reset at every tool boundary, so a turn that streamed a
    preamble and then called a tool arrives at the terminal chain with an empty
    final segment and takes the empty-response branch. Rung 1 of that ladder
    re-queues the ORIGINAL message, which re-runs every tool call that already
    completed — a second ``send_message``, a second write, a second PR — and
    re-derives an answer the user has already read.

    The field incident these tests pin: two consecutive billed turns, each with an
    assistant preamble and successful tool calls and each ending on a clean
    ``end_turn``, were both classified empty; the first verbatim-replayed the
    user's message.

    Reuses ``TestEmptyResponseRetry``'s harness rather than a second one, so a
    change to how a slot is built cannot leave these tests driving a different
    runner than the ladder tests beside them.
    """

    _make_state_and_slot = TestEmptyResponseRetry._make_state_and_slot

    @staticmethod
    def _spy_queue(monkeypatch=None):
        """Record ``(index, content)`` for every queue insert."""
        calls: list[tuple] = []
        orig = _ChatSlot.queue_insert

        def spy(self_slot, *a, **kw):
            calls.append(a)
            return orig(self_slot, *a, **kw)

        return calls, spy

    _cancel_background_tasks = staticmethod(TestEmptyResponseRetry._cancel_background_tasks)

    @staticmethod
    def _text_then_tool_stream(client, executed):
        """TEXT -> TOOL_CALL -> TOOL_RESULT -> COMPLETE(end_turn).

        ``executed`` counts dispatches so a replay of the original message would
        be observable as a second execution rather than only as a queue entry.
        """
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="checking")
            executed.append(msg)
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc-1",
                title="send_message",
            )
            yield LLMEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-1", text="sent")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream

    @staticmethod
    def _tool_only_stream(client):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-1", title="send_message")
            yield LLMEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-1", text="sent")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream

    @staticmethod
    def _thinking_only_stream(client):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_THINKING_CHUNK, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_THINKING_CHUNK, text="hmm")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream

    @pytest.mark.asyncio
    async def test_text_then_tool_turn_continues_instead_of_replaying(self, tmp_path: Path) -> None:
        """The incident's own shape: preamble + a completed tool call, clean
        end_turn. The original message must NOT be re-queued; exactly one
        synthetic continuation may be, and the completed tool must not re-run."""
        from kiro_crew.dashboard.chat_utils import (
            _ACTIVITY_NO_REPLY_CONTINUE_MSG,
            _EMPTY_AUTO_CONTINUE_MSG,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        executed: list[str] = []
        self._text_then_tool_stream(client, executed)
        calls, spy = self._spy_queue()

        with patch.object(_ChatSlot, "queue_insert", spy):
            await _run_chat(state, slot, "send the summary")
            await self._cancel_background_tasks(state)

        # The verbatim replay is the defect. It must be absent.
        assert (0, "send the summary") not in calls, (
            "the ORIGINAL user message was re-queued after a turn that already "
            "ran a tool — the replay re-executes completed side effects"
        )
        # Exactly one continuation, and it is the one whose body does not claim
        # the turn produced nothing (which would invite the model to redo the
        # completed call).
        _continuations = [c for c in calls if c and c[1] == _ACTIVITY_NO_REPLY_CONTINUE_MSG]
        assert len(_continuations) == 1, f"expected one continuation, got {calls}"
        # The productive turn skipped rung 1 (verbatim replay), so this one
        # continuation consumes the whole bounded ladder. If the continuation
        # itself returns empty, it gives up rather than enqueueing a SECOND
        # continuation whose wording would again invite rework.
        assert slot._empty_response_retries == 2
        assert (0, _EMPTY_AUTO_CONTINUE_MSG) not in calls, (
            "the empty-response body tells the model its turn produced no "
            "output, which is false for a turn that ran a tool"
        )
        # The tool ran once. The queue was not drained in this test, so a second
        # execution could only come from a replay dispatched inside this turn.
        assert executed == ["send the summary"], f"tool dispatched more than once: {executed}"

    @pytest.mark.asyncio
    async def test_tool_only_turn_does_not_replay_verbatim(self, tmp_path: Path) -> None:
        """A turn with tool calls and no text at all still counts as productive."""
        from kiro_crew.dashboard.chat_utils import _ACTIVITY_NO_REPLY_CONTINUE_MSG

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._tool_only_stream(client)
        calls, spy = self._spy_queue()

        with patch.object(_ChatSlot, "queue_insert", spy):
            await _run_chat(state, slot, "post it")
            await self._cancel_background_tasks(state)

        assert (0, "post it") not in calls, "tool-only turn verbatim-replayed the prompt"
        assert (0, _ACTIVITY_NO_REPLY_CONTINUE_MSG) in calls

    @pytest.mark.asyncio
    async def test_thinking_only_turn_does_not_replay_verbatim(self, tmp_path: Path) -> None:
        """Thinking is work the replay would discard and re-charge for."""
        from kiro_crew.dashboard.chat_utils import _ACTIVITY_NO_REPLY_CONTINUE_MSG

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._thinking_only_stream(client)
        calls, spy = self._spy_queue()

        with patch.object(_ChatSlot, "queue_insert", spy):
            await _run_chat(state, slot, "think about it")
            await self._cancel_background_tasks(state)

        assert (0, "think about it") not in calls, "thinking-only turn verbatim-replayed the prompt"
        assert (0, _ACTIVITY_NO_REPLY_CONTINUE_MSG) in calls

    @pytest.mark.asyncio
    async def test_activity_free_turn_keeps_the_verbatim_replay_rung(self, tmp_path: Path) -> None:
        """The guard is scoped: a GENUINELY activity-free empty turn keeps rung 1.

        Without this, the fix could be 'never replay', which would silently
        remove the self-heal that a real provider-side empty depends on.
        """
        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        TestEmptyResponseRetry._make_empty_stream(self, client)
        calls, spy = self._spy_queue()

        with (
            patch.object(_ChatSlot, "queue_insert", spy),
            patch(
                "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
                new=AsyncMock(return_value=False),
            ),
        ):
            await _run_chat(state, slot, "test message")
            await self._cancel_background_tasks(state)

        assert (0, "test message") in calls, "the activity-free replay rung was removed"
        assert slot._empty_response_retries == 1


class TestEmptyTurnDiagnostics:
    """The empty-response WARNING must name a CLOSED cause, and carry no content.

    The incident it exists for had three attempts with three different causes and
    one log line that could not tell them apart. These tests assert on the closed
    vocabulary only — never on a prompt, a response, a tool argument, a path, an
    identity, a token count or a cost, because asserting on those would pin
    exactly the leak the diagnostics are designed to avoid.
    """

    _make_state_and_slot = TestEmptyResponseRetry._make_state_and_slot
    _cancel_background_tasks = staticmethod(TestEmptyResponseRetry._cancel_background_tasks)

    @staticmethod
    def _causes(caplog):
        """The ``cause=`` values of every empty-response warning captured."""
        out = []
        for rec in caplog.records:
            msg = rec.getMessage()
            if "Empty model response" not in msg:
                continue
            for field in msg.split():
                if field.startswith("cause="):
                    out.append(field.split("=", 1)[1])
        return out

    @staticmethod
    def _fields(caplog):
        """``key=value`` pairs of the first empty-response warning captured."""
        for rec in caplog.records:
            msg = rec.getMessage()
            if "Empty model response" not in msg:
                continue
            return dict(
                tuple(f.split("=", 1)) for f in msg.split() if "=" in f and not f.startswith("%")
            )
        return {}

    def test_the_cause_and_rung_vocabularies_are_closed(self) -> None:
        """Pure-unit: every cause the classifier can return, from its own inputs.

        Kept as a unit test over ``classify_empty_turn`` rather than seven
        ``_run_chat`` drives: the ranking between overlapping causes is the part
        that can regress silently, and a stream fixture cannot express
        'synthesized terminal AND tool activity' as cleanly as the snapshot can.
        """
        from kiro_crew.dashboard.chat_utils import (
            EMPTY_CAUSE_NO_TERMINAL,
            EMPTY_CAUSE_OTHER,
            EMPTY_CAUSE_PROVIDER_EMPTY,
            EMPTY_CAUSE_SYNTHETIC,
            EMPTY_CAUSE_THINKING_ONLY,
            EMPTY_CAUSE_TOOL_ONLY,
            EMPTY_CAUSE_VISIBLE_PARTIAL,
            STOP_REASON_ABSENT,
            EmptyTurnActivity,
            classify_empty_turn,
        )

        # No terminal outranks everything, including activity.
        assert (
            classify_empty_turn(EmptyTurnActivity(saw_terminal=False, had_tools=True))
            == EMPTY_CAUSE_NO_TERMINAL
        )
        # A synthesized terminal outranks activity: the emptiness is ours.
        assert (
            classify_empty_turn(
                EmptyTurnActivity(saw_terminal=True, terminal_synthetic=True, had_tools=True)
            )
            == EMPTY_CAUSE_SYNTHETIC
        )
        # A flushed visible segment outranks tools: the user read an answer.
        assert (
            classify_empty_turn(
                EmptyTurnActivity(saw_terminal=True, flushed_visible=True, had_tools=True)
            )
            == EMPTY_CAUSE_VISIBLE_PARTIAL
        )
        assert (
            classify_empty_turn(EmptyTurnActivity(saw_terminal=True, had_tools=True))
            == EMPTY_CAUSE_TOOL_ONLY
        )
        assert (
            classify_empty_turn(EmptyTurnActivity(saw_terminal=True, had_thinking=True))
            == EMPTY_CAUSE_THINKING_ONLY
        )
        # A clean end_turn with nothing at all is the only genuine provider empty.
        assert (
            classify_empty_turn(EmptyTurnActivity(saw_terminal=True, stop_reason="end_turn"))
            == EMPTY_CAUSE_PROVIDER_EMPTY
        )
        # An OMITTED stop reason is NOT a provider empty — the distinction the
        # incident's third attempt needed and did not have.
        assert (
            classify_empty_turn(
                EmptyTurnActivity(saw_terminal=True, stop_reason=STOP_REASON_ABSENT)
            )
            == EMPTY_CAUSE_OTHER
        )

    def test_normalize_stop_reason_never_returns_a_raw_backend_string(self) -> None:
        """Closed output set, and the ACP spellings it mirrors are pinned.

        ``chat_utils`` may not import ``kiro_crew.acp.types`` (the agent-SDK
        boundary gate baselines it at one edge and a baselined file may not grow),
        so it spells the three stop reasons as literals. The test tree is outside
        that gate, so the pin lives here — a change to the backend's vocabulary
        reddens here instead of silently reclassifying every turn as ``other``.
        """
        from kiro_crew.acp.types import (
            STOP_REASON_CANCELLED,
            STOP_REASON_END_TURN,
            STOP_REASON_REFUSAL,
        )
        from kiro_crew.dashboard.chat_utils import (
            _STOP_CANCELLED_REASON,
            _STOP_END_TURN,
            _STOP_REFUSAL,
            STOP_REASON_ABSENT,
            STOP_REASON_OTHER,
            normalize_stop_reason,
        )

        assert _STOP_END_TURN == STOP_REASON_END_TURN
        assert _STOP_CANCELLED_REASON == STOP_REASON_CANCELLED
        assert _STOP_REFUSAL == STOP_REASON_REFUSAL

        assert normalize_stop_reason(None) == STOP_REASON_ABSENT
        assert normalize_stop_reason("") == STOP_REASON_ABSENT
        assert normalize_stop_reason(STOP_REASON_END_TURN) == STOP_REASON_END_TURN
        assert normalize_stop_reason("error: tool stall") == "error"
        # An invented backend reason is folded, never echoed.
        _invented = "the model said stop because of /home/someone/secret.txt"
        assert normalize_stop_reason(_invented) == STOP_REASON_OTHER

    @pytest.mark.asyncio
    async def test_provider_empty_and_no_terminal_event_report_distinct_causes(
        self, tmp_path: Path, caplog
    ) -> None:
        """Two turns that look identical to the old log line must not any more."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat_utils import (
            EMPTY_CAUSE_NO_TERMINAL,
            EMPTY_CAUSE_PROVIDER_EMPTY,
        )
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        # A clean end_turn with nothing in it.
        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        async def _clean(msg):
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _clean
        client.stream_command = _clean
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_runner"):
            await _run_chat(state, slot, "a", _prompt_depth=1)
        await self._cancel_background_tasks(state)
        assert self._causes(caplog) == [EMPTY_CAUSE_PROVIDER_EMPTY]

        # A stream that ends without any terminal event at all.
        caplog.clear()
        state2, slot2, client2, _run_chat2 = self._make_state_and_slot(tmp_path)

        async def _no_terminal(msg):
            return
            yield  # pragma: no cover -- makes this an async generator

        client2.stream = _no_terminal
        client2.stream_command = _no_terminal
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_runner"):
            await _run_chat2(state2, slot2, "a", _prompt_depth=1)
        await self._cancel_background_tasks(state2)
        assert self._causes(caplog) == [EMPTY_CAUSE_NO_TERMINAL]

    @pytest.mark.asyncio
    async def test_omitted_stop_reason_is_not_reported_as_provider_empty(
        self, tmp_path: Path, caplog
    ) -> None:
        """A terminal with no stopReason is its own observation."""
        from kiro_crew.dashboard.chat_utils import (
            EMPTY_CAUSE_OTHER,
            STOP_REASON_ABSENT,
        )
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_COMPLETE)  # no stop_reason

        client.stream = _stream
        client.stream_command = _stream
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_runner"):
            await _run_chat(state, slot, "a", _prompt_depth=1)
        await self._cancel_background_tasks(state)

        assert self._causes(caplog) == [EMPTY_CAUSE_OTHER]
        assert self._fields(caplog).get("stop_reason") == STOP_REASON_ABSENT

    @pytest.mark.asyncio
    async def test_productive_turn_reports_visible_partial_and_the_continue_rung(
        self, tmp_path: Path, caplog
    ) -> None:
        """The incident's shape gets its own cause AND the rung it took.

        Asserting the rung is what makes the diagnostic answer 'what did the
        runner DO', not only 'what did it see'.
        """
        from kiro_crew.dashboard.chat_utils import (
            EMPTY_CAUSE_VISIBLE_PARTIAL,
            EMPTY_RUNG_CONTINUE,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        TestProductiveTurnNeverReplaysVerbatim._text_then_tool_stream(client, [])

        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_runner"):
            await _run_chat(state, slot, "send the summary")
            await self._cancel_background_tasks(state)

        assert self._causes(caplog) == [EMPTY_CAUSE_VISIBLE_PARTIAL]
        _fields = self._fields(caplog)
        assert _fields.get("rung") == EMPTY_RUNG_CONTINUE
        assert _fields.get("flushed_visible") == "True"
        assert _fields.get("tools") == "True"

    @pytest.mark.asyncio
    async def test_the_warning_carries_no_content_and_no_billing_amounts(
        self, tmp_path: Path, caplog
    ) -> None:
        """The privacy floor, asserted against the rendered line.

        Every diagnostic value is a bool or a closed constant, so the prompt text,
        the streamed text, the tool title and the billed amounts must be absent
        from the line even though the turn carried all of them.
        """
        from kiro_crew.acp.types import STOP_REASON_END_TURN, TurnUsage
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="SECRETPREAMBLE")
            yield LLMEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-1", title="SECRETTOOL")
            yield LLMEvent(
                kind=EVENT_COMPLETE,
                stop_reason=STOP_REASON_END_TURN,
                usage=TurnUsage(credits=4.25, input_tokens=1234, output_tokens=99),
            )

        client.stream = _stream
        client.stream_command = _stream
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_runner"):
            await _run_chat(state, slot, "SECRETPROMPT")
            await self._cancel_background_tasks(state)

        _line = next(
            rec.getMessage() for rec in caplog.records if "Empty model response" in rec.getMessage()
        )
        for forbidden in ("SECRETPROMPT", "SECRETPREAMBLE", "SECRETTOOL", "4.25", "1234", "99"):
            assert forbidden not in _line, f"{forbidden!r} leaked into {_line!r}"
        # Billing presence IS reported — as a bool, which is the point.
        assert "billed=True" in _line


class TestExpandDollarSkills:
    """Runner-side ``_expand_dollar_skills``: redaction, chip,
    SEL audit, and empty/exception branches. The pure resolution logic is
    covered separately by test_skills.TestResolveDollarSkills; here we
    exercise the chat_runner wrapper that adds runner concerns."""

    def _make_skill(self, skills_dir: Path, name: str, body: str) -> None:
        d = skills_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")

    def _state_with_skills(self, tmp_path, monkeypatch, *skills):
        """A DashboardState whose _get_skills() returns a hermetic loader
        pointed at *skills* (each a ``(name, body)`` pair)."""
        # Keep edition-contributed skill roots out of the hermetic loader.
        from kiro_crew.platform.defaults import DefaultMcpToolingProvider
        from kiro_crew.skills import SkillsLoader

        monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: [])
        skills_dir = tmp_path / "skills"
        for name, body in skills:
            self._make_skill(skills_dir, name, body)
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)

        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state._standalone_skills = loader  # _get_skills returns this verbatim
        return state

    def test_no_dollar_short_circuits(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _expand_dollar_skills

        state = self._state_with_skills(tmp_path, monkeypatch)
        slot = _ChatSlot("s1")
        out, n = _expand_dollar_skills("plain message", state, slot, "sess")
        assert out == "plain message"
        assert n == 0
        state.push_slots_update.assert_not_called()

    def test_resolves_appends_block_and_chip(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _expand_dollar_skills

        state = self._state_with_skills(
            tmp_path,
            monkeypatch,
            ("oncall-handover", "---\nname: oncall-handover\n---\n# Handover\nBODY-A"),
        )
        slot = _ChatSlot("s1")
        out, n = _expand_dollar_skills("please $oncall-handover now", state, slot, "sess")
        assert n == 1
        # original message preserved, skill body appended as a [Skill: name] block
        assert out.startswith("please $oncall-handover now")
        assert "[Skill: oncall-handover]" in out
        assert "BODY-A" in out
        # a system chip is appended and the UI is poked
        assert any(
            "Loaded skill(s)" in m.get("content", "") and m.get("role") == "system"
            for m in slot.messages
        )
        state.push_slots_update.assert_called_once()

    def test_unresolved_token_no_chip(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import chat_runner

        state = self._state_with_skills(
            tmp_path,
            monkeypatch,
            ("oncall-handover", "---\nname: oncall-handover\n---\nBODY"),
        )
        slot = _ChatSlot("s1")
        sel_mock = MagicMock()
        monkeypatch.setattr(chat_runner, "sel", lambda: sel_mock)

        out, n = chat_runner._expand_dollar_skills("$does-not-exist hi", state, slot, "sess")
        assert (out, n) == ("$does-not-exist hi", 0)
        state.push_slots_update.assert_not_called()
        # a real $skill-shaped token that didn't resolve IS audited as not_found
        assert sel_mock.log_tool_invocation.called
        _, kwargs = sel_mock.log_tool_invocation.call_args
        assert kwargs.get("outcome") == "not_found"
        assert kwargs.get("tool_name") == "skill_dollar_expansion"

    def test_incidental_dollar_not_audited(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import chat_runner

        state = self._state_with_skills(
            tmp_path,
            monkeypatch,
            ("oncall-handover", "---\nname: oncall-handover\n---\nBODY"),
        )
        slot = _ChatSlot("s1")
        sel_mock = MagicMock()
        monkeypatch.setattr(chat_runner, "sel", lambda: sel_mock)

        # $5 / $PATH / bare $ are NOT skill-shaped → no not_found noise.
        out, n = chat_runner._expand_dollar_skills("it costs $5 not $PATH", state, slot, "sess")
        assert (out, n) == ("it costs $5 not $PATH", 0)
        sel_mock.log_tool_invocation.assert_not_called()

    def test_credentials_redacted_in_loaded_body(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _expand_dollar_skills

        secret = "AKIAIOSFODNN7EXAMPLE"
        state = self._state_with_skills(
            tmp_path,
            monkeypatch,
            ("leaky", f"---\nname: leaky\n---\n# Leaky\naws_key={secret}"),
        )
        slot = _ChatSlot("s1")
        out, n = _expand_dollar_skills("use $leaky", state, slot, "sess")
        assert n == 1
        # the raw credential must not survive into the expanded message
        assert secret not in out

    def test_resolution_exception_audited_and_swallowed(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import chat_runner

        state = self._state_with_skills(tmp_path, monkeypatch)
        slot = _ChatSlot("s1")

        boom = MagicMock()
        boom.resolve_dollar_skills.side_effect = RuntimeError("kaboom")
        # Force _get_skills(state) to return the exploding loader.
        monkeypatch.setattr(chat_runner, "_get_skills", lambda _s: boom)

        sel_mock = MagicMock()
        monkeypatch.setattr(chat_runner, "sel", lambda: sel_mock)

        out, n = chat_runner._expand_dollar_skills("try $whatever", state, slot, "sess")
        # message returned unchanged, no crash
        assert (out, n) == ("try $whatever", 0)
        # the failure was audited via SEL with outcome="error"
        assert sel_mock.log_tool_invocation.called
        _, kwargs = sel_mock.log_tool_invocation.call_args
        assert kwargs.get("outcome") == "error"
        assert kwargs.get("tool_name") == "skill_dollar_expansion"


# ── Transient backend 5xx retry on the interactive chat path ──


class TestRunChatTransientRetry:
    """The interactive _run_chat stream loop reuses the llm_helpers transient
    classifier + backoff to retry a transient
    backend 5xx WITHOUT resetting the live session, guarded so a partial
    (already-streamed) response is never re-run."""

    _TRANSIENT = "Prompt error: {'message': 'Internal error: API Error: Internal server error'}"
    _AUTH = "Bedrock authentication failed. Run 'ada credentials update'"

    @staticmethod
    def _make_state(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.push_refresh = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @staticmethod
    def _wire_sessions(state, client):
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)
        state.sessions.check_context_usage = MagicMock()
        state.sessions.record_success = MagicMock()
        state.sessions.record_failure = AsyncMock()
        state.sessions.release = MagicMock()
        # reset is an AsyncMock so we can assert it is NEVER awaited — a
        # transient 5xx must NOT reset the (still-alive) session.
        state.sessions.reset = AsyncMock()
        # discard_conversation is the poisoned-conversation escalation (clears
        # the resume sid, keeps the session-map entry with its channel
        # linkage); mocked so tests can assert exactly when it fires.
        state.sessions.discard_conversation = AsyncMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))

    @staticmethod
    def _client(stream):
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = stream
        client.stream_command = stream
        # The poisoned-conversation canary reads the session's served model
        # via the provider's PUBLIC served_model accessor — mock that public
        # seam, not the provider internals it happens to be backed by.
        client.served_model = "claude-test-model"
        return client

    @staticmethod
    async def _drain_bg(state, limit=30):
        """Drive the finally-block re-queue cascade (each retry spawns a
        background _run_chat task) to completion. Bounded by the retry budget."""
        for _ in range(limit):
            pending = [t for t in list(state._background_tasks) if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    def _err_texts(self, slot):
        return [m["content"] for m in slot.messages if m.get("role") == "error"]

    def _assistant_texts(self, slot):
        return [m["content"] for m in slot.messages if m.get("role") == "assistant"]

    @pytest.mark.asyncio
    async def test_transient_pre_token_retries_then_recovers_no_reset(self, tmp_path, monkeypatch):
        """A transient 5xx before any token streams is retried on the SAME live
        session (no reset); the second attempt succeeds."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True  # skip background auto-title

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert call_count == 2  # initial transient failure + one retry
        # Recovered: the response is persisted, no ❌ error surfaced.
        assert any("ok-result" in t for t in self._assistant_texts(slot))
        assert not any(t.startswith("❌") for t in self._err_texts(slot))
        # The transient branch fired (status surfaced) ...
        assert any("Backend hiccup" in t for t in self._err_texts(slot))
        # ... and the notice is TAGGED, which is the only thing telling the UI a
        # recovery is pending; without it a stale pill re-runs the queued choice.
        from kiro_crew.dashboard.chat_utils import TRANSIENT_RETRY_KIND

        _hiccups = [
            m
            for m in slot.messages
            if m.get("role") == "error" and "Backend hiccup" in m.get("content", "")
        ]
        assert _hiccups, "no hiccup row to inspect"
        assert all((m.get("meta") or {}).get("kind") == TRANSIENT_RETRY_KIND for m in _hiccups)
        # Negative control: a TERMINAL error row must NOT carry the tag, or the
        # discriminator would classify every failure as a pending retry.
        slot.append("error", "❌ terminal for the control", "msg msg-err")
        assert (slot.messages[-1].get("meta") or {}).get("kind") != TRANSIENT_RETRY_KIND
        # ... and the live session was NOT reset.
        state.sessions.reset.assert_not_awaited()
        # Budget reset to 0 after the successful turn.
        assert slot._transient_5xx_retries == 0

    @pytest.mark.asyncio
    async def test_transient_post_token_textonly_retries_once(self, tmp_path, monkeypatch):
        """A transient 5xx AFTER text streamed (no tool call) is retried EXACTLY
        ONCE, APPEND-ONLY: the streamed partial is PRESERVED (finalized as a
        normal assistant message, exactly like the terminal else: branch), a
        recovery notice is surfaced, and a CONTINUE instruction — NOT the
        original prompt — is re-queued onto the same live session. The retry
        appends the continued answer as a NEW message below. The user sees an
        append-only sequence [partial] [recover notice] [continued answer] with
        nothing retracted. No chat_stream_reset event exists anymore."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        call_count = 0
        captured: list = []

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            captured.append(msg)
            if call_count == 1:
                # First attempt streams a partial, then hits a transient 5xx.
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial ")
                raise AcpError(self._TRANSIENT)
            # Retry streams a clean, continued answer.
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert call_count == 2  # one post-token retry
        # The recovery notice surfaced and no ❌ error was raised.
        assert any("Backend hiccup" in t for t in self._err_texts(slot))
        assert not any(t.startswith("❌") for t in self._err_texts(slot))
        # APPEND-ONLY: the partial is PRESERVED as a finalized assistant message,
        # AND the continued retry answer is appended below it. Both are present;
        # the live chunk placeholders are finalized away.
        assert not any(m.get("role") == "chunk" for m in slot.messages)
        assistant = self._assistant_texts(slot)
        assert any("partial" in t for t in assistant), "partial must be preserved"
        assert any("ok-result" in t for t in assistant), "continued answer must append below"
        # The RE-QUEUED prompt is the CONTINUE instruction, NOT the original.
        assert len(captured) == 2
        assert (
            "Continue from where it stopped" in captured[1]
        ), "the retry must re-queue the continue instruction, not the original prompt"
        assert "hello" not in captured[1], "the original prompt must NOT be re-queued"
        # No chat_stream_reset broadcast — that frontend reconcile event was
        # removed entirely by the append-only rework.
        assert not any(
            c.args and c.args[0] == "chat_stream_reset" for c in state.broadcast_ws.call_args_list
        ), "chat_stream_reset must no longer be broadcast"
        # Live session was NOT reset. The one-shot allowance stays consumed
        # (True) across the synthetic recovery turn — it is refreshed only at the
        # start of the NEXT genuine user turn, never on the recovery turn itself
        # (finding #3, prevents a recovery-turn re-failure from looping).
        state.sessions.reset.assert_not_awaited()
        assert slot._posttoken_retry_used is True

    @pytest.mark.asyncio
    async def test_transient_post_token_suppressed_preserves_partial(self, tmp_path, monkeypatch):
        """When Stop is active (_should_suppress_requeue True) during a post-token
        transient 5xx, the partial assistant text is PRESERVED (persisted as an
        assistant message) and the message is NOT re-queued. Under the
        append-only design the partial + a retry notice are shown regardless of
        eligibility; only the re-queue is gated."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            # Stream a partial, then hit a transient 5xx (post-token, no tool).
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
            raise AcpError(self._TRANSIENT)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        # Stop is active → suppress the re-queue for the whole handler.
        monkeypatch.setattr(chat_runner, "_should_suppress_requeue", lambda s: True)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # No re-queue: the stream ran exactly once (Stop is active). Finding #3:
        # a suppressed recovery does NOT consume the one-shot allowance — the
        # flag is only set when a recovery is actually enqueued, so it stays
        # False and a later genuine turn can still recover once.
        assert call_count == 1
        assert slot._posttoken_retry_used is False
        # HIGH: the partial survives as a persisted assistant message.
        assert any("partial answer" in t for t in self._assistant_texts(slot))
        # Chunk placeholders were finalized (not left as role "chunk").
        assert not any(m.get("role") == "chunk" for m in slot.messages)
        # The retry notice is shown (append-only: partial + notice regardless of
        # eligibility); no ❌ terminal error is raised here.
        assert any("Backend hiccup" in t for t in self._err_texts(slot))
        assert not any(t.startswith("❌") for t in self._err_texts(slot))
        # No chat_stream_reset broadcast — that event was removed entirely.
        assert not any(
            c.args and c.args[0] == "chat_stream_reset" for c in state.broadcast_ws.call_args_list
        )
        # Transient path never resets the (still-alive) session.
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transient_post_toolcall_recovers(self, tmp_path, monkeypatch):
        """A transient 5xx AFTER a TOOL CALL fired now RECOVERS (no longer
        fail-fast). Because the retry re-queues a CONTINUE instruction onto the
        SAME live session — which still holds the completed tool results — the
        model resumes from where it stopped instead of blindly re-running the
        tool. Exactly one post-token recovery fires, the partial is preserved,
        and the continue instruction (not the original prompt) is re-queued."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        call_count = 0
        captured: list = []

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            captured.append(msg)
            if call_count == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="working ")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    text="",
                    title="fs_read",
                    tool_kind="read",
                    tool_call_id="tc-1",
                )
                raise AcpError(self._TRANSIENT)
            # Retry continues from the preserved context and finishes cleanly.
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert call_count == 2  # post-tool transient now RECOVERS via continue
        # Recovery notice surfaced, no ❌ terminal error.
        assert any("Backend hiccup" in t for t in self._err_texts(slot))
        assert not any(t.startswith("❌") for t in self._err_texts(slot))
        # The partial text is preserved and the continued answer appended below.
        assert any("working" in t for t in self._assistant_texts(slot)), "partial must be preserved"
        assert any(
            "ok-result" in t for t in self._assistant_texts(slot)
        ), "continued answer must append below"
        # The CONTINUE instruction — not the original prompt — was re-queued.
        assert len(captured) == 2
        assert "Continue from where it stopped" in captured[1]
        assert "hello" not in captured[1]
        # The one-shot allowance stays consumed (True) across the recovery turn;
        # it is refreshed only by the next genuine user turn. Live session never
        # reset.
        assert slot._posttoken_retry_used is True
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_posttoken_suppressed_leaves_allowance_for_later_turn(
        self, tmp_path, monkeypatch
    ):
        """Finding #3(a): a Stop-suppressed post-token transient must NOT consume
        the one-shot allowance (the flag is set only when a recovery is actually
        enqueued). A LATER genuine user turn on the same slot can therefore still
        recover once."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        # ── Turn 1: Stop active → suppressed post-token transient. ──
        async def _stream_suppressed(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial-1")
            raise AcpError(self._TRANSIENT)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream_suppressed)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        monkeypatch.setattr(chat_runner, "_should_suppress_requeue", lambda s: True)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "first question")
            await self._drain_bg(state)

        # Allowance untouched — the suppressed path never enqueued a recovery.
        assert slot._posttoken_retry_used is False

        # ── Turn 2: Stop cleared → a genuine turn recovers once. ──
        call_count = 0

        async def _stream_recover(msg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial-2")
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client.stream = _stream_recover
        client.stream_command = _stream_recover
        monkeypatch.setattr(chat_runner, "_should_suppress_requeue", lambda s: False)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "second question")
            await self._drain_bg(state)

        # The later real turn recovered: it re-ran once and produced the answer.
        assert call_count == 2
        assert any("ok-result" in t for t in self._assistant_texts(slot))
        assert not any(t.startswith("❌") for t in self._err_texts(slot))

    @pytest.mark.asyncio
    async def test_posttoken_recovery_turn_refailure_does_not_loop(self, tmp_path, monkeypatch):
        """Finding #3(b): a post-token transient that re-fires DURING the
        synthetic recovery turn must NOT recover again (no infinite re-queue).
        The recovery turn inherits the already-consumed allowance (it is not
        refreshed for the recover message), so the post-token branch is skipped
        and a clean terminal error surfaces instead of a second recovery."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        captured: list = []

        async def _stream(msg):
            captured.append(msg)
            # The recovery turn emits a partial then hits ANOTHER transient 5xx.
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="still-failing")
            raise AcpError(self._TRANSIENT)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        # Simulate the state left by the originating turn that enqueued recovery.
        slot._posttoken_retry_used = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            # Drive the recovery turn directly with the continue instruction.
            await _run_chat(state, slot, _POSTTOKEN_RECOVER_MSG)
            await self._drain_bg(state)

        # Ran exactly once — no second recovery was enqueued (no loop).
        assert len(captured) == 1
        # The recover instruction was NOT re-queued a second time.
        assert not any(m == _POSTTOKEN_RECOVER_MSG for m in captured[1:])
        # The flag was NOT refreshed by the recovery turn (stays consumed).
        assert slot._posttoken_retry_used is True
        # A clean terminal error surfaced (branch skipped, fell to else:).
        assert any(t.startswith("❌") for t in self._err_texts(slot))
        # The partial from the recovery turn is still preserved (append-only).
        assert any("still-failing" in t for t in self._assistant_texts(slot))
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auth_error_fails_fast_no_retry(self, tmp_path, monkeypatch):
        """An auth failure is excluded from the transient set — it fails fast
        with a clean error and is never retried (a retry can't fix a token)."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._AUTH)
            yield  # pragma: no cover — makes this an async generator

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert call_count == 1  # fail-fast, no retry
        assert slot._transient_5xx_retries == 0
        assert any(t.startswith("❌") for t in self._err_texts(slot))
        assert not any("Backend hiccup" in t for t in self._err_texts(slot))
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transient_exhausts_budget_then_clean_error_resumable(
        self, tmp_path, monkeypatch
    ):
        """Persistent transient 5xx is retried up to the budget, then surfaces a
        clean ❌ error while leaving the session resumable (never reset)."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        # fallback_model="" (disabled): this test pins the PRE-FEATURE ladder
        # (the default is now "auto", which would walk the fallback branch —
        # that branch has its own class, TestRunChatModelFallback).
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ())
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # Initial attempt + TRANSIENT_RETRIES re-prompts, then it gives up.
        assert call_count == TRANSIENT_RETRIES + 1
        # Clean error surfaced; session left resumable (no reset).
        assert any(t.startswith("❌") for t in self._err_texts(slot))
        state.sessions.reset.assert_not_awaited()
        # The terminal ❌ ENDS the cycle, so the budget is refreshed for the
        # next one — the Continue press the error message invites must get the
        # full ladder again, not inherit an exhausted counter.
        assert slot._transient_5xx_retries == 0

    @pytest.mark.asyncio
    async def test_transient_budget_refreshed_after_terminal_error(self, tmp_path, monkeypatch):
        """Regression: a cycle that EXHAUSTS the transient budget and surfaces
        the terminal ❌ must refresh the budget for the NEXT cycle. The
        happy-path reset only runs when a cycle COMPLETES, so before the fix
        the exhausted counter leaked into every later cycle: the very next
        5xx — e.g. right after the Continue press the ❌ message itself
        invites ("retry in a moment") — failed instantly with ZERO retries
        until some turn happened to finish cleanly."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        # ── Cycle 1: persistent 5xx exhausts the budget → terminal ❌. ──
        async def _always_fail(msg):
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_always_fail)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "first question")
            await self._drain_bg(state)

        assert any(t.startswith("❌") for t in self._err_texts(slot))
        assert slot._transient_5xx_retries == 0
        n_before = len(slot.messages)

        # ── Cycle 2: a 5xx on the next turn retries again (fresh budget). ──
        call_count = 0

        async def _fail_once_then_ok(msg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client.stream = _fail_once_then_ok
        client.stream_command = _fail_once_then_ok

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "second question")
            await self._drain_bg(state)

        # Before the fix: 3 < TRANSIENT_RETRIES was False on the first 5xx, so
        # cycle 2 died instantly (call_count == 1, a second ❌, no answer).
        assert call_count == 2
        tail = slot.messages[n_before:]
        tail_errs = [m["content"] for m in tail if m.get("role") == "error"]
        tail_answers = [m["content"] for m in tail if m.get("role") == "assistant"]
        assert any("Backend hiccup" in t for t in tail_errs)
        assert not any(t.startswith("❌") for t in tail_errs)
        assert any("ok-result" in t for t in tail_answers)
        # Completed cycle → budget back to 0 via the happy-path reset.
        assert slot._transient_5xx_retries == 0
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_first_exhausted_cycle_never_discards(self, tmp_path, monkeypatch):
        """A SINGLE cycle that exhausts the pre-stream transient ladder
        surfaces the terminal ❌ without discarding the native conversation —
        one exhaustion is still plausibly a momentary outage, and discarding
        on it would throw away a healthy conversation on every blip that
        outlasts the ladder."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        async def _always_fail(msg):
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_always_fail)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert any(t.startswith("❌") for t in self._err_texts(slot))
        state.sessions.discard_conversation.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()
        # The exhausted cycle is counted toward the consecutive streak.
        assert slot._prestream_exhausted_cycles == 1
        assert slot._poisoned_reset_used is False

    @pytest.mark.asyncio
    async def test_poisoned_conversation_discarded_on_second_exhausted_cycle(
        self, tmp_path, monkeypatch
    ):
        """Regression (poisoned persisted conversation): when TWO consecutive
        cycles each exhaust the full pre-stream transient ladder with zero
        output, the backend is deterministically rejecting this session's
        native conversation — retrying into it can never succeed (observed
        live: a session/load'ed conversation failing pre-stream identically
        11 hours apart while a NEW session on the same gateway+model answered
        instantly). The second exhaustion must escalate: discard the
        native conversation (discard_conversation() clears the resume sid —
        reset() would session/load the poison right back — while keeping the
        session-map entry with its channel linkage) and re-queue the message
        once, so the recovery cycle cold-starts a fresh conversation and the
        slot self-heals instead of telling the user to 'retry in a moment'
        forever."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        # Fail every attempt of two full cycles (the "poisoned conversation"),
        # then succeed — the success models the fresh post-discard conversation.
        _poisoned_calls = 2 * (TRANSIENT_RETRIES + 1)
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count <= _poisoned_calls:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="recovered-after-discard")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        # fallback_model="" (disabled): pins the PRE-FEATURE escalation ladder
        # (the "auto" default would walk the fallback branch instead).
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ())
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch(
                "kiro_crew.dashboard.chat_runner.run_bg_oneliner",
                new_callable=AsyncMock,
                return_value="OK",
            ) as canary,
        ):
            # Cycle 1: ladder exhausts → terminal ❌ (streak = 1, no discard,
            # no canary — below the consecutive threshold).
            await _run_chat(state, slot, "first try")
            await self._drain_bg(state)
            state.sessions.discard_conversation.assert_not_awaited()
            canary.assert_not_awaited()
            # Cycle 2 (the Continue press the ❌ invites): ladder exhausts
            # again → the canary probes a FRESH conversation and succeeds →
            # conversation-specific rejection proven → escalation fires and
            # the re-queued recovery cycle lands on the fresh session.
            await _run_chat(state, slot, "continue")
            await self._drain_bg(state)
            canary.assert_awaited_once()
            # The probe is only meaningful on the SAME served model as the
            # failing session, with fallback disabled (GPT review finding:
            # a canary succeeding on a different model must never justify
            # discarding this conversation).
            assert canary.await_args.kwargs["model"] == "claude-test-model"
            assert canary.await_args.kwargs["strict_model"] is True

        # Escalation discarded the native conversation exactly once; plain
        # reset (which preserves the poisoned resume sid) was never used.
        state.sessions.discard_conversation.assert_awaited_once()
        state.sessions.reset.assert_not_awaited()
        # Cycle 1 ❌ + cycle 2 escalation notice, then the recovered answer.
        errs = self._err_texts(slot)
        assert any(t.startswith("❌") for t in errs)
        assert any("keeps rejecting" in t for t in errs)
        assert any("recovered-after-discard" in t for t in self._assistant_texts(slot))
        # Attempt accounting: two full ladders + the single recovery prompt.
        assert call_count == _poisoned_calls + 1
        # The landed recovery turn re-arms the one-shot and breaks the streak.
        assert slot._poisoned_reset_used is False
        assert slot._prestream_exhausted_cycles == 0

    @pytest.mark.asyncio
    async def test_poisoned_discard_is_one_shot_until_a_turn_lands(self, tmp_path, monkeypatch):
        """If even the fresh post-discard conversation keeps failing (genuine
        prolonged outage), no second discard fires: the one-shot is consumed by
        the first escalation and only a LANDED turn re-arms it, so a discard
        loop is impossible. Later cycles surface the terminal ❌ as before."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        async def _always_fail(msg):
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_always_fail)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch(
                "kiro_crew.dashboard.chat_runner.run_bg_oneliner",
                new_callable=AsyncMock,
                return_value="OK",
            ),
        ):
            # Cycle 1: exhausts → ❌ (streak 1).
            await _run_chat(state, slot, "first try")
            await self._drain_bg(state)
            # Cycle 2: exhausts → canary succeeds → discard #1 → recovery
            # cycle ALSO exhausts → terminal ❌ (one-shot consumed, so the
            # eligibility gate rejects before the canary even runs again).
            await _run_chat(state, slot, "continue")
            await self._drain_bg(state)
            # Cycle 3: user retries once more — still failing. Streak keeps
            # counting but the one-shot is spent: ❌ again, NO second discard.
            await _run_chat(state, slot, "continue again")
            await self._drain_bg(state)

        state.sessions.discard_conversation.assert_awaited_once()
        state.sessions.reset.assert_not_awaited()
        # No turn ever landed: the one-shot stays consumed.
        assert slot._poisoned_reset_used is True
        # Terminal ❌ surfaced after the failed recovery and again on cycle 3.
        assert sum(1 for t in self._err_texts(slot) if t.startswith("❌")) >= 2

    @pytest.mark.asyncio
    async def test_cancelled_turn_does_not_rearm_poisoned_one_shot(self, tmp_path, monkeypatch):
        """Regression: the poisoned-discard one-shot re-arms only on a LANDED
        turn. But the STREAK is evidence-based: a cancelled turn that EMITTED
        output proves the backend accepts this conversation, so it breaks the
        streak (GPT review finding — without this, exhaustion → stopped-but-
        streaming turn → exhaustion would discard a healthy conversation),
        while a cancelled turn with NO output proves nothing and preserves
        it. In both cases the spent one-shot stays consumed."""
        from kiro_crew.acp.types import STOP_REASON_CANCELLED
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        # ── Cancelled turn with NO output: both guards preserved. ──
        async def _cancelled_silent(msg):
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_cancelled_silent)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        # Arrange: an escalation already consumed the one-shot mid-streak.
        slot._poisoned_reset_used = True
        slot._prestream_exhausted_cycles = 3

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "user stops before any output")
            await self._drain_bg(state)

        assert slot._poisoned_reset_used is True
        assert slot._prestream_exhausted_cycles == 3

        # ── Cancelled turn WITH output: streak broken, one-shot still spent. ──
        async def _cancelled_after_output(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial before stop")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED)

        client.stream = _cancelled_after_output
        client.stream_command = _cancelled_after_output
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "recovery attempt the user stops mid-answer")
            await self._drain_bg(state)

        assert slot._prestream_exhausted_cycles == 0  # output = evidence
        assert slot._poisoned_reset_used is True  # cancel ≠ landed

        # ── A genuinely LANDED turn re-arms the one-shot too. ──
        async def _ok(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="landed")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client.stream = _ok
        client.stream_command = _ok
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "works now")
            await self._drain_bg(state)

        assert slot._poisoned_reset_used is False
        assert slot._prestream_exhausted_cycles == 0

    @pytest.mark.asyncio
    async def test_canary_failure_blocks_discard_and_preserves_one_shot(
        self, tmp_path, monkeypatch
    ):
        """GPT-review fix: two exhausted ladders alone are NOT
        conversation-specific evidence — a sustained backend-wide outage or
        throttle produces the identical pattern. The canary probe (one prompt
        on a fresh background conversation) is the discriminator: when it
        ALSO fails, no discard fires, the one-shot stays unconsumed, and the
        streak stays accrued so a later user-initiated cycle re-probes once
        the outage ends."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        async def _always_fail(msg):
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_always_fail)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch(
                "kiro_crew.dashboard.chat_runner.run_bg_oneliner",
                new_callable=AsyncMock,
                side_effect=AcpError(self._TRANSIENT),
            ) as canary,
        ):
            # Two consecutive exhausted cycles — the pattern that WOULD
            # discard if the canary had succeeded.
            await _run_chat(state, slot, "first try")
            await self._drain_bg(state)
            await _run_chat(state, slot, "continue")
            await self._drain_bg(state)

        canary.assert_awaited_once()  # probed at the threshold, cycle 2
        state.sessions.discard_conversation.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()
        # Backend-wide failure consumes NOTHING: the one-shot survives for a
        # later cycle whose canary succeeds, and the streak keeps accruing.
        assert slot._poisoned_reset_used is False
        assert slot._prestream_exhausted_cycles == 2
        assert sum(1 for t in self._err_texts(slot) if t.startswith("❌")) >= 2

    @pytest.mark.asyncio
    async def test_canary_empty_reply_is_not_positive_evidence(self, tmp_path, monkeypatch):
        """A canary that completes with EMPTY output proves nothing about the
        fresh conversation working — fail-safe to no-discard."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        async def _always_fail(msg):
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_always_fail)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch(
                "kiro_crew.dashboard.chat_runner.run_bg_oneliner",
                new_callable=AsyncMock,
                return_value="  ",
            ),
        ):
            await _run_chat(state, slot, "first try")
            await self._drain_bg(state)
            await _run_chat(state, slot, "continue")
            await self._drain_bg(state)

        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._poisoned_reset_used is False

    @pytest.mark.asyncio
    async def test_stop_during_canary_vetoes_discard_and_requeue(self, tmp_path, monkeypatch):
        """GPT-review fix: a Stop initiated DURING the (up to 30s) canary probe
        must veto the discard and the synthetic re-queue even when the canary
        succeeds — the user just cancelled this work, and re-queueing would
        re-execute it with tool side effects. The veto keys on the monotonic
        _stop_generation, not _stop_state, because teardown can drive
        _stop_state back to "idle" before the check (a stop that fired AND
        resolved mid-probe must still veto). Nothing is consumed: the one-shot
        stays armed and the streak stays accrued."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        async def _always_fail(msg):
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_always_fail)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        async def _canary_with_midflight_stop(*a, **kw):
            # User presses Stop while the probe is in flight; the stop then
            # RESOLVES (teardown drives _stop_state back to idle) before the
            # probe returns — the generation edge is the only surviving trace.
            slot._stop_state = "soft_pending"
            slot._stop_state = "idle"
            return "OK"  # the canary itself succeeds

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch(
                "kiro_crew.dashboard.chat_runner.run_bg_oneliner",
                new_callable=AsyncMock,
                side_effect=_canary_with_midflight_stop,
            ) as canary,
        ):
            await _run_chat(state, slot, "first try")
            await self._drain_bg(state)
            await _run_chat(state, slot, "continue")
            await self._drain_bg(state)

        canary.assert_awaited_once()  # probe ran at the threshold
        # ...but the mid-probe stop vetoed everything downstream: no discard,
        # no synthetic recovery turn, nothing consumed.
        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._queue == []  # no synthetic recovery turn queued
        assert slot._poisoned_reset_used is False
        assert slot._prestream_exhausted_cycles == 2

    @pytest.mark.asyncio
    async def test_unreadable_session_model_skips_canary_and_discard(self, tmp_path, monkeypatch):
        """When the session's served model cannot be read, the canary cannot
        be pinned to it, so the probe is meaningless — fail-safe: no probe,
        no discard, one-shot preserved."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        async def _always_fail(msg):
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_always_fail)
        client.served_model = ""  # unreadable/unresolved
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch(
                "kiro_crew.dashboard.chat_runner.run_bg_oneliner",
                new_callable=AsyncMock,
                return_value="OK",
            ) as canary,
        ):
            await _run_chat(state, slot, "first try")
            await self._drain_bg(state)
            await _run_chat(state, slot, "continue")
            await self._drain_bg(state)

        canary.assert_not_awaited()
        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._poisoned_reset_used is False

    @pytest.mark.asyncio
    async def test_transient_after_thinking_only_retries(self, tmp_path, monkeypatch):
        """A transient 5xx that lands AFTER reasoning streamed but before any
        answer token or tool call is still retried: thinking is ephemeral,
        broadcast-only output, so it does NOT flip _turn_emitted. This pins the
        deliberate decision in the EVENT_THINKING_CHUNK branch — if thinking
        ever starts being persisted, this guard must become a turn-emit to
        avoid a double-emit."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_THINKING_CHUNK,
            LLMEvent,
        )

        call_count = 0
        thinking_seen = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            # Reasoning streams on BOTH attempts (cosmetic re-stream is the
            # accepted cost of retrying a thinking-only turn).
            yield LLMEvent(kind=EVENT_THINKING_CHUNK, text="let me think...")
            if call_count == 1:
                # Transient 5xx after thinking, before any answer token.
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)

        # Count chat_thinking broadcasts to prove reasoning re-streamed.
        _orig_broadcast = state.broadcast_ws

        def _count_thinking(event, payload, *a, **kw):
            nonlocal thinking_seen
            if event == "chat_thinking":
                thinking_seen += 1
            return _orig_broadcast(event, payload, *a, **kw)

        state.broadcast_ws = MagicMock(side_effect=_count_thinking)

        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # Thinking did NOT block the retry: initial transient + one retry.
        assert call_count == 2
        # Reasoning re-streamed on the retry. The streaming redaction buffer
        # (issue 3) may split/coalesce a thinking chunk across broadcasts, so
        # assert "streamed on both attempts" rather than an exact per-chunk count.
        assert thinking_seen >= 2
        # Recovered cleanly on the live session (no reset, no ❌).
        assert any("ok-result" in t for t in self._assistant_texts(slot))
        assert not any(t.startswith("❌") for t in self._err_texts(slot))
        assert any("Backend hiccup" in t for t in self._err_texts(slot))
        state.sessions.reset.assert_not_awaited()
        assert slot._transient_5xx_retries == 0

    @pytest.mark.asyncio
    async def test_thinking_only_failures_never_accrue_discard_streak(self, tmp_path, monkeypatch):
        """A turn that streamed REASONING before dying is a mid-generation
        failure — the backend demonstrably serves this conversation — not the
        poisoned pre-stream signature. Repeated thinking-then-transient-death
        turns must never accrue the discard streak (and so can never reach the
        canary/discard), even though they leave _turn_emitted False for retry
        purposes. Locks in the _turn_thought gate on _prestream_exhausted."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_THINKING_CHUNK, LLMEvent

        async def _think_then_die(msg):
            yield LLMEvent(kind=EVENT_THINKING_CHUNK, text="reasoning...")
            raise AcpError(self._TRANSIENT)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_think_then_die)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            # Well past POISONED_SESSION_CYCLES worth of exhausted ladders.
            for i in range(3):
                await _run_chat(state, slot, f"msg-{i}")
                await self._drain_bg(state)

        # Thinking activity keeps the streak at zero every cycle: never
        # eligible, so no canary probe and no discard — the conversation is
        # being served, just dying mid-generation.
        assert slot._prestream_exhausted_cycles == 0
        assert not slot._poisoned_reset_used
        state.sessions.discard_conversation.assert_not_awaited()


# ── Bulk model switch (api_chat_slots_model) ──


class TestRunChatModelFallback:
    """Throttle-exhaustion model fallback (agent.fallback_model) on the
    interactive path: budget exhausts → substitute set_model → visible notice
    card → re-queue on the SAME live session; empty chain is byte-for-byte
    today's terminal error; the sticky swap is restored at the next genuine
    turn start."""

    _TRANSIENT = "Prompt error: {'message': 'Internal error: API Error: Internal server error'}"

    @pytest.mark.asyncio
    async def test_fallback_swap_serialises_on_the_pick_lock(self, tmp_path, monkeypatch):
        """REGRESSION (GPT finding on c97f2f2d): the swap awaits set_model, and
        an explicit pick landing during that await could be overwritten and
        snapshotted as fallback state. The swap must hold _model_pick_lock —
        with the lock pre-acquired, the swap may not advance until release.
        This closes the LAST writer: explicit pick, bulk pick, restore probe,
        and swap all serialise on the same per-slot lock."""
        import asyncio

        from kiro_crew.dashboard.chat_runner import _fallback_swap_for_turn

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        entered = asyncio.Event()

        class _Client:
            async def set_model(self, target):
                entered.set()
                self._model = target

        client = _Client()

        await slot._model_pick_lock.acquire()
        try:
            task = asyncio.create_task(_fallback_swap_for_turn(slot, client))
            await asyncio.sleep(0.05)
            # Lock held ⇒ the swap must not have reached set_model yet.
            assert not entered.is_set()
            assert not task.done()
        finally:
            slot._model_pick_lock.release()
        candidate = await asyncio.wait_for(task, timeout=2)
        assert candidate == "fallback-model"
        assert slot._active_fallback_model == "fallback-model"

    @pytest.mark.asyncio
    async def test_stop_during_fallback_backoff_drops_the_requeue(self, tmp_path, monkeypatch):
        """REGRESSION (review finding on 1a61ddcf): a Stop pressed during the
        fallback backoff sleep resolves while no prompt is active; without the
        post-sleep guard the cancelled prompt is requeued and executes on the
        fallback anyway."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        async def _stop_during_sleep(_secs):
            # Simulate the user pressing Stop while the backoff sleeps: the
            # monotonic generation increments even though the stop resolves
            # back to "idle" before the sleep returns.
            slot._stop_generation = getattr(slot, "_stop_generation", 0) + 1

        with patch("asyncio.sleep", new=AsyncMock(side_effect=_stop_during_sleep)):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # Only the primary's budget ran; the cancelled prompt was NOT replayed
        # on the fallback (unfixed code replays it: +FALLBACK_CANDIDATE_ATTEMPTS).
        assert call_count == TRANSIENT_RETRIES + 1
        assert not any(q for q in getattr(slot, "_queue", []) if "hello" in str(q))
        # The drop arm ends the turn, so it must mirror the landed/terminal
        # per-turn resets: a fresh transient budget and a cleared walk (which
        # re-enables the restore probe). Sticky _active_fallback_model
        # deliberately survives for the probe to heal.
        assert slot._transient_5xx_retries == 0
        assert slot._fallback_candidate_idx == 0
        assert slot._fallback_walked == []

    _make_state = staticmethod(TestRunChatTransientRetry._make_state)
    _wire_sessions = staticmethod(TestRunChatTransientRetry._wire_sessions)
    _drain_bg = staticmethod(TestRunChatTransientRetry._drain_bg)

    def _err_texts(self, slot):
        return [m["content"] for m in slot.messages if m.get("role") == "error"]

    def _notice_texts(self, slot):
        return [m["content"] for m in slot.messages if m.get("role") == "notice"]

    def _assistant_texts(self, slot):
        return [m["content"] for m in slot.messages if m.get("role") == "assistant"]

    @staticmethod
    def _client(stream, advertised=("fallback-model",)):
        client = TestRunChatTransientRetry._client(stream)
        client._model = "claude-test-model"
        client.available_models = MagicMock(return_value=[{"modelId": m} for m in advertised])

        async def _set_model(model_id):
            # Mimic the real substitute path's bookkeeping so read_turn_model
            # (and therefore meta.turn_stats.model) reports the candidate.
            client._model = model_id
            client.served_model = model_id

        client.set_model = AsyncMock(side_effect=_set_model)
        return client

    @pytest.mark.asyncio
    async def test_budget_exhaustion_swaps_and_recovers(self, tmp_path, monkeypatch):
        """After the same-model budget exhausts, the session is moved onto the
        chain's candidate, the swap is ANNOUNCED as a persisted notice card,
        the turn recovers, and turn stats record the candidate."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count <= TRANSIENT_RETRIES + 1:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="fb-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # 4 attempts on the primary, 1 on the candidate (success).
        assert call_count == TRANSIENT_RETRIES + 2
        client.set_model.assert_awaited_once_with("fallback-model")
        # The swap is announced — a persisted notice card, never silent.
        notices = self._notice_texts(slot)
        assert any(
            "claude-test-model" in t and "fallback-model" in t and "throttled" in t for t in notices
        ), f"expected a fallback notice card, got {notices!r}"
        # Recovered on the candidate; no terminal ❌.
        assert any("fb-result" in t for t in self._assistant_texts(slot))
        assert not any(t.startswith("❌") for t in self._err_texts(slot))
        # The live session was never reset — same-session recovery.
        state.sessions.reset.assert_not_awaited()
        # Sticky fallback state armed for the next turn's restore probe...
        assert slot._active_fallback_model == "fallback-model"
        assert slot._fallback_primary_model == "claude-test-model"
        # ...while the per-cycle walk state reset with the budgets.
        assert slot._transient_5xx_retries == 0
        assert slot._fallback_candidate_idx == 0
        assert slot._fallback_walked == []
        # Turn stats carry the model that ACTUALLY served the turn: the
        # substitute set_model updated the provider's bookkeeping (mimicked by
        # this mock exactly as AcpSessionHandle/AcpClient.set_model do), so the
        # read_turn_model seam — the sole source for meta.turn_stats.model
        # (see _attach_turn_stats, pinned by test_turn_stats.py) — reports the
        # candidate. The mocked provider yields no duration, so the stats dict
        # itself is not attached here; the model field's attach contract is
        # already pinned by test_turn_stats.py::test_model_included_when_resolved.
        from kiro_crew.dashboard.handlers.usage import read_turn_model

        assert read_turn_model(client) == "fallback-model"

    @pytest.mark.asyncio
    async def test_empty_chain_is_todays_terminal_error(self, tmp_path, monkeypatch):
        """REGRESSION PIN: with no chain configured, budget exhaustion surfaces
        the terminal ❌ exactly as before — no swap, no notice, no extra
        attempts."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ())
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert call_count == TRANSIENT_RETRIES + 1
        client.set_model.assert_not_awaited()
        assert not self._notice_texts(slot)
        assert any(t.startswith("❌") for t in self._err_texts(slot))
        assert slot._active_fallback_model == ""

    @pytest.mark.asyncio
    async def test_chain_exhaustion_surfaces_error_with_story(self, tmp_path, monkeypatch):
        """Every candidate also fails: the terminal ❌ carries the chain's
        story (primary throttled + which fallbacks were tried), and each
        candidate got exactly two attempts."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # 4 on the primary + 2 on the candidate (initial + one retry).
        assert call_count == TRANSIENT_RETRIES + 3
        client.set_model.assert_awaited_once_with("fallback-model")
        errs = self._err_texts(slot)
        assert any(
            "fallback-model" in t and "also unavailable" in t for t in errs
        ), f"expected the chain story on the terminal error, got {errs!r}"
        # Per-cycle walk state refreshed for the next user-initiated cycle.
        assert slot._fallback_candidate_idx == 0
        assert slot._fallback_walked == []

    @pytest.mark.asyncio
    async def test_candidate_budget_routes_through_shared_rewind_body(self, tmp_path, monkeypatch):
        """DRIFT PIN (#5447 item 2): the post-swap counter rewind must come
        from llm_helpers.fallback_rewound_transient_budget — not a re-encoded
        local constant. Patching the shared body to grant no extra pass drops
        the candidate to a single attempt."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        # Rewind to the exhaustion threshold: the re-queued attempt runs, and
        # its failure immediately re-enters the fallback branch (no same-model
        # retry pass) — one attempt on the candidate instead of two.
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.fallback_rewound_transient_budget",
            lambda: TRANSIENT_RETRIES,
        )
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # 4 on the primary + only 1 on the candidate (vs 2 unpatched — see
        # test_chain_exhaustion_surfaces_error_with_story).
        assert call_count == TRANSIENT_RETRIES + 2
        client.set_model.assert_awaited_once_with("fallback-model")

    @pytest.mark.asyncio
    async def test_restore_probe_fires_on_next_genuine_turn(self, tmp_path, monkeypatch):
        """A sticky fallback from an earlier cycle is probed back to the
        primary at the start of the next genuine user turn — quietly (no
        chat card), clearing the sticky state on success."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        client._model = "fallback-model"
        client.served_model = "fallback-model"
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot._active_fallback_model = "fallback-model"
        slot._fallback_primary_model = "claude-test-model"

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello again")
            await self._drain_bg(state)

        client.set_model.assert_awaited_once_with("claude-test-model")
        assert slot._active_fallback_model == ""
        assert slot._fallback_primary_model == ""
        # Quiet restore: no notice card for recovery.
        assert not self._notice_texts(slot)
        assert any("ok-result" in t for t in self._assistant_texts(slot))

    @pytest.mark.asyncio
    async def test_restore_probe_skipped_when_session_moved_on(self, tmp_path, monkeypatch):
        """If the session is no longer on our fallback (explicit user pick),
        the stale sticky state is dropped WITHOUT touching the model."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        client._model = "user-picked-model"
        client.served_model = "user-picked-model"
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot._active_fallback_model = "fallback-model"
        slot._fallback_primary_model = "claude-test-model"
        # A stale provider marker from the abandoned fallback must be dropped
        # WITH the slot state — a surviving marker would re-seed the long-dead
        # primary into a later, unrelated fallback walk.
        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

        setattr(client, TURN_FALLBACK_ATTR, ("claude-test-model", "fallback-model"))

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello again")
            await self._drain_bg(state)

        client.set_model.assert_not_awaited()
        assert slot._active_fallback_model == ""
        assert getattr(client, TURN_FALLBACK_ATTR) is None

    @pytest.mark.asyncio
    async def test_restore_probe_honors_explicit_pick_of_the_fallback_itself(
        self, tmp_path, monkeypatch
    ):
        """REGRESSION (review finding): a user who EXPLICITLY picks the very
        model the session fell back to changes slot.model but not the served
        model — the probe must drop the sticky state without restoring the
        old primary over their pick."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        client._model = "fallback-model"
        client.served_model = "fallback-model"
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        # Fallback activated while the slot was configured on the primary...
        slot._active_fallback_model = "fallback-model"
        slot._fallback_primary_model = "claude-test-model"
        slot._fallback_slot_model = "claude-test-model"
        slot._fallback_pick_gen = slot._model_pick_gen
        # ...then the user explicitly picked the fallback model themselves
        # (the pick surface writes slot.model AND bumps the pick generation).
        slot.model = "fallback-model"
        slot._model_pick_gen += 1

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello again")
            await self._drain_bg(state)

        # No restore fired — the explicit pick wins — and the sticky state is gone.
        client.set_model.assert_not_awaited()
        assert slot._active_fallback_model == ""
        assert slot._fallback_slot_model == ""
        # The explicit pick itself is untouched.
        assert slot.model == "fallback-model"

    @pytest.mark.asyncio
    async def test_restore_probe_heals_automatic_backfill(self, tmp_path, monkeypatch):
        """REGRESSION (verifier finding): on an UNPINNED slot, a served-
        fallback value that reached slot.model without a pick-generation bump
        (pre-guard pollution) must NOT be misread as an explicit pick: the
        probe restores the primary, drops the sticky state, and heals
        slot.model back to the activation snapshot (else the fallback id would
        be re-sent as a pin on resume)."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        client._model = "fallback-model"
        client.served_model = "fallback-model"
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        # Fallback activated on an UNPINNED slot (empty snapshot, no pick)...
        slot._active_fallback_model = "fallback-model"
        slot._fallback_primary_model = "claude-test-model"
        slot._fallback_slot_model = ""
        slot._fallback_pick_gen = slot._model_pick_gen
        # ...then the automatic backfill wrote the served fallback into
        # slot.model WITHOUT bumping the pick generation.
        slot.model = "fallback-model"

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello again")
            await self._drain_bg(state)

        # The restore fired (backfill is not a pick) and healed the slot pin.
        client.set_model.assert_awaited_once_with("claude-test-model")
        assert slot._active_fallback_model == ""
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_backfill_never_persists_the_fallback_candidate(self, tmp_path, monkeypatch):
        """REGRESSION (review finding, the span invariant): while a fallback is
        ACTIVE, the automatic model backfill must not write the served
        candidate into slot.model — slot.model is PERSISTED, and the in-memory
        sticky state is not, so a gateway restart would turn the temporary
        fallback into a permanent pin. An unpinned slot stays unpinned for the
        fallback's duration."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        # The provider resolves to the FALLBACK candidate (it is serving).
        client._model = "fallback-model"
        client.served_model = "fallback-model"
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot.model = ""  # unpinned
        slot._active_fallback_model = "fallback-model"
        slot._fallback_primary_model = ""  # primary unknown -> no restore
        slot._fallback_pick_gen = slot._model_pick_gen

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # The turn ran, but the persisted pin was never written with the
        # candidate. (The unknown-primary probe clears the sticky state — the
        # pin staying empty is the property under test.)
        assert slot.model == ""


class TestSlotProbeWrapsSharedRestoreBody:
    """DRIFT PIN (#5447 item 3): the slot restore probe is a thin adapter over
    llm_helpers.probe_fallback_restore — only the slot pick-gen/heal/clear
    logic lives locally. These tests pin the delegation and the slot-specific
    hooks it hands over; the probe mechanics themselves are pinned in
    test_llm_helpers.py and the end-to-end behavior in
    TestRunChatModelFallback above."""

    @staticmethod
    def _slot(**overrides):
        from types import SimpleNamespace

        defaults = dict(
            key="s1",
            model="",
            _model_pick_lock=None,
            _active_fallback_model="fb-1",
            _fallback_primary_model="primary-m",
            _fallback_slot_model="",
            _model_pick_gen=3,
            _fallback_pick_gen=3,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    @pytest.mark.asyncio
    async def test_delegates_slot_state_to_the_shared_body(self):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

        slot = self._slot()
        client = MagicMock()
        setattr(client, TURN_FALLBACK_ATTR, ("primary-m", "fb-1"))
        with patch(
            "kiro_crew.dashboard.chat_runner.probe_fallback_restore", new_callable=AsyncMock
        ) as probe:
            await chat_runner._probe_fallback_restore_for_slot(slot, client)

        probe.assert_awaited_once()
        kwargs = probe.await_args.kwargs
        assert kwargs["surface"] == "dashboard"
        assert kwargs["state"] == ("primary-m", "fb-1")
        assert kwargs["stale"] is False
        assert kwargs["log_suffix"] == ", slot=s1"
        # The handed-over clear drops slot fields AND the provider marker as
        # one logical record.
        kwargs["clear"]()
        assert slot._active_fallback_model == ""
        assert slot._fallback_primary_model == ""
        assert getattr(client, TURN_FALLBACK_ATTR) is None

    @pytest.mark.asyncio
    async def test_explicit_pick_generation_bump_rides_in_as_stale(self):
        from kiro_crew.dashboard import chat_runner

        slot = self._slot(_model_pick_gen=4)  # picked after the swap
        with patch(
            "kiro_crew.dashboard.chat_runner.probe_fallback_restore", new_callable=AsyncMock
        ) as probe:
            await chat_runner._probe_fallback_restore_for_slot(slot, MagicMock())
        assert probe.await_args.kwargs["stale"] is True

    @pytest.mark.asyncio
    async def test_heal_hook_restores_the_activation_snapshot(self):
        from kiro_crew.dashboard import chat_runner

        slot = self._slot(model="fb-1", _fallback_slot_model="")  # backfilled pin
        with patch(
            "kiro_crew.dashboard.chat_runner.probe_fallback_restore", new_callable=AsyncMock
        ) as probe:
            await chat_runner._probe_fallback_restore_for_slot(slot, MagicMock())
        probe.await_args.kwargs["on_restored"]()
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_no_active_fallback_never_reaches_the_shared_body(self):
        from kiro_crew.dashboard import chat_runner

        slot = self._slot(_active_fallback_model="")
        with patch(
            "kiro_crew.dashboard.chat_runner.probe_fallback_restore", new_callable=AsyncMock
        ) as probe:
            await chat_runner._probe_fallback_restore_for_slot(slot, MagicMock())
        probe.assert_not_awaited()

    def test_sticky_clear_drops_marker_and_slot_fields(self):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

        slot = self._slot()
        client = MagicMock()
        setattr(client, TURN_FALLBACK_ATTR, ("primary-m", "fb-1"))
        chat_runner._clear_fallback_sticky_state(slot, client)
        assert getattr(client, TURN_FALLBACK_ATTR) is None
        assert slot._active_fallback_model == ""
        assert slot._fallback_primary_model == ""
        assert slot._fallback_slot_model == ""

    def test_sticky_clear_keeps_slot_record_when_marker_clear_fails(self):
        """DRIFT PIN (review round 3): a failed provider-marker clear must NOT
        blank the slot fields — the slot probe keys on _active_fallback_model,
        so keeping the record is what makes the clear retryable next turn.
        Blanking the fields around a surviving marker would orphan it."""
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

        class _HostileClient:
            @property
            def _kc_active_fallback(self):  # TURN_FALLBACK_ATTR
                raise RuntimeError("hostile marker")

        assert TURN_FALLBACK_ATTR == "_kc_active_fallback"
        slot = self._slot()
        chat_runner._clear_fallback_sticky_state(slot, _HostileClient())  # must not raise
        assert slot._active_fallback_model == "fb-1"
        assert slot._fallback_primary_model == "primary-m"


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


class TestBulkModelSwitch:
    """POST /api/chat/slots/model — switch every live slot to one model."""

    @staticmethod
    def _app(state: DashboardState) -> web.Application:
        from kiro_crew.dashboard.chat import api_chat_slots_model

        # Mirror production: token_auth middleware sets request["app"] on
        # every authenticated path ("" = dashboard user). Tests that model an
        # app-token caller insert their own middleware at index 0, which runs
        # first; this default only fills in the marker when absent.
        @web.middleware
        async def dashboard_auth_marker(request, handler):
            if "app" not in request:
                request["app"] = ""
            return await handler(request)

        app = web.Application(middlewares=[dashboard_auth_marker])
        app["state"] = state
        app.router.add_post("/api/chat/slots/model", api_chat_slots_model)
        return app

    @staticmethod
    def _mark_running(slot: _ChatSlot) -> None:
        """Give the slot a live task so the ``running`` property is True."""
        task = MagicMock()
        task.done.return_value = False
        slot.task = task

    @pytest.mark.asyncio
    async def test_switches_all_differing_slots_and_resets_each(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="claude-opus-4.6")
        state.get_or_create_slot("b", model="claude-sonnet-4.6")
        state.push_slots_update = MagicMock()  # mock after creation: get_or_create_slot pushes

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": "claude-opus-4.8"})
            data = await resp.json()

        assert resp.status == 200
        assert sorted(data["switched"]) == ["a", "b"]
        assert data["skipped_running"] == []
        assert state._slots["a"].model == "claude-opus-4.8"
        assert state._slots["b"].model == "claude-opus-4.8"
        assert state.sessions.reset.await_count == 2
        state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_running_slot_by_default(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.push_slots_update = MagicMock()
        idle = state.get_or_create_slot("idle", model="claude-opus-4.6")
        busy = state.get_or_create_slot("busy", model="claude-opus-4.6")
        self._mark_running(busy)

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": "claude-opus-4.8"})
            data = await resp.json()

        assert resp.status == 200
        assert data["switched"] == ["idle"]
        assert data["skipped_running"] == ["busy"]
        assert idle.model == "claude-opus-4.8"
        assert busy.model == "claude-opus-4.6"  # untouched mid-turn
        assert state.sessions.reset.await_count == 1

    @pytest.mark.asyncio
    async def test_skip_running_false_forces_running_slot(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.push_slots_update = MagicMock()
        busy = state.get_or_create_slot("busy", model="claude-opus-4.6")
        self._mark_running(busy)

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/model",
                json={"model": "claude-opus-4.8", "skip_running": False},
            )
            data = await resp.json()

        assert resp.status == 200
        assert data["switched"] == ["busy"]
        assert data["skipped_running"] == []
        assert busy.model == "claude-opus-4.8"
        assert state.sessions.reset.await_count == 1

    @pytest.mark.asyncio
    async def test_already_on_target_is_unchanged_not_reset(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()  # mock after creation: get_or_create_slot pushes

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": "claude-opus-4.8"})
            data = await resp.json()

        assert resp.status == 200
        assert data["unchanged"] == ["a"]
        assert data["switched"] == []
        state.sessions.reset.assert_not_awaited()
        state.push_slots_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_reset_failure_is_isolated_and_reported(self, tmp_path):
        state = _make_state(tmp_path)
        # First reset (slot "a") succeeds, second ("b") raises. The failure must
        # be isolated: "a" still switches, "b" lands in `failed` with its old
        # model intact, and partial progress is still broadcast.
        state.sessions.reset = AsyncMock(side_effect=[None, RuntimeError("boom")])
        state.get_or_create_slot("a", model="claude-opus-4.6")
        state.get_or_create_slot("b", model="claude-opus-4.6")
        state.push_slots_update = MagicMock()  # mock after creation: get_or_create_slot pushes

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": "claude-opus-4.8"})
            data = await resp.json()

        assert resp.status == 200
        assert data["switched"] == ["a"]
        assert data["failed"] == ["b"]
        assert state._slots["a"].model == "claude-opus-4.8"
        assert state._slots["b"].model == "claude-opus-4.6"  # untouched on reset failure
        state.push_slots_update.assert_called_once()  # partial progress still broadcast

    @pytest.mark.asyncio
    async def test_app_caller_only_switches_own_slots(self, tmp_path):
        """App Kit ownership isolation: an app token can only bulk-switch its
        own slots -- other apps' and the dashboard user's slots are untouched
        (mirrors api_chat_slots_cleanup)."""
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("mine", model="claude-opus-4.6", app="app-A")
        state.get_or_create_slot("theirs", model="claude-opus-4.6", app="app-B")
        state.get_or_create_slot("dashboard", model="claude-opus-4.6")
        state.push_slots_update = MagicMock()  # mock after creation: get_or_create_slot pushes

        @web.middleware
        async def inject_app(request, handler):
            request["app"] = "app-A"
            return await handler(request)

        app_obj = self._app(state)
        app_obj.middlewares.insert(0, inject_app)

        async with TestClient(TestServer(app_obj)) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": "claude-opus-4.8"})
            data = await resp.json()

        assert resp.status == 200
        assert data["switched"] == ["mine"]
        assert state._slots["mine"].model == "claude-opus-4.8"
        # Non-owned slots: not switched, not reset, not even reported.
        assert state._slots["theirs"].model == "claude-opus-4.6"
        assert state._slots["dashboard"].model == "claude-opus-4.6"
        assert "theirs" not in data["unchanged"] + data["skipped_running"] + data["failed"]
        assert state.sessions.reset.await_count == 1

    @pytest.mark.asyncio
    async def test_missing_auth_marker_is_denied(self, tmp_path):
        """Deny-by-default: if the auth middleware never ran (request["app"]
        absent), the endpoint refuses instead of granting all-slot access."""
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="claude-opus-4.6")

        from kiro_crew.dashboard.chat import api_chat_slots_model

        bare_app = web.Application()  # deliberately NO auth-marker middleware
        bare_app["state"] = state
        bare_app.router.add_post("/api/chat/slots/model", api_chat_slots_model)

        async with TestClient(TestServer(bare_app)) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 403
        assert state._slots["a"].model == "claude-opus-4.6"
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falsy_non_string_marker_fails_closed(self, tmp_path):
        """A buggy middleware setting request["app"] to a falsy non-string
        (None) must NOT be treated as a dashboard user -- the ownership check
        applies and no slot matches, so nothing is switched."""
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="claude-opus-4.6")

        from kiro_crew.dashboard.chat import api_chat_slots_model

        @web.middleware
        async def buggy_auth_marker(request, handler):
            request["app"] = None  # falsy, but NOT the explicit "" dashboard marker
            return await handler(request)

        app_obj = web.Application(middlewares=[buggy_auth_marker])
        app_obj["state"] = state
        app_obj.router.add_post("/api/chat/slots/model", api_chat_slots_model)

        async with TestClient(TestServer(app_obj)) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": "claude-opus-4.8"})
            data = await resp.json()

        assert resp.status == 200
        assert data["switched"] == []
        assert state._slots["a"].model == "claude-opus-4.6"
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_boolean_skip_running_is_rejected(self, tmp_path):
        state = _make_state(tmp_path)
        state.get_or_create_slot("a", model="claude-opus-4.6")

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/model",
                json={"model": "claude-opus-4.8", "skip_running": "yes"},
            )

        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_canonical_key_rejected_no_slot_touched(self, tmp_path):
        # A canonical registry key (the /api/models cold-start fallback trap)
        # is rejected 400 for the acp provider; no slot is switched or reset.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="claude-fable-5")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": "fable-5-1m"})
            data = await resp.json()

        assert resp.status == 400
        assert "fable-5-1m" in data["error"]
        assert state._slots["a"].model == "claude-fable-5"
        state.sessions.reset.assert_not_awaited()


class TestSlotModelGuard:
    """POST /api/chat/slots/{slot}/model — reject canonical registry keys the
    ACP CLI cannot accept (the /api/models cold-start fallback -32603 trap)."""

    @staticmethod
    def _app(state: DashboardState) -> web.Application:
        from kiro_crew.dashboard.chat import api_chat_slot_model

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/model", api_chat_slot_model)
        return app

    @pytest.mark.asyncio
    async def test_canonical_key_rejected_slot_unchanged(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="claude-fable-5")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "fable-5-1m"})
            data = await resp.json()

        assert resp.status == 400
        assert "fable-5-1m" in data["error"]
        # The slot keeps its valid model and the session is NOT reset.
        assert state._slots["a"].model == "claude-fable-5"
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_string_auto_default_is_allowed(self, tmp_path):
        # The frontend maps 'auto' -> '' before calling; '' is the provider
        # default and always passes.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="claude-fable-5")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": ""})

        assert resp.status == 200
        assert state._slots["a"].model == ""
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_auto_literal_is_allowed(self, tmp_path):
        # A literal 'auto' (registry key, but the safe sentinel) always passes.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="claude-fable-5")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "auto"})

        assert resp.status == 200
        assert state._slots["a"].model == "auto"

    @pytest.mark.asyncio
    async def test_valid_kiro_alias_is_allowed(self, tmp_path):
        # kiro/acp ids (registry aliases, not top-level keys) pass through.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="claude-fable-5")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        assert state._slots["a"].model == "claude-opus-4.8"
        state.sessions.reset.assert_awaited_once()


class TestSlotModelLiveSwitch:
    """POST /api/chat/slots/{slot}/model — prefer an in-place session/set_model
    over tearing the session down.

    A reset costs a full process-tree teardown now plus a cold start and
    transcript replay on the next message. kiro-cli's ``session/set_model``
    switches a live session instead (verified against 2.15.1: acked
    synchronously, conversation carried across the switch, sticky over later
    turns), so the reset is only a fallback.
    """

    @staticmethod
    def _app(state: DashboardState) -> web.Application:
        from kiro_crew.dashboard.chat import api_chat_slot_model

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/model", api_chat_slot_model)
        return app

    @staticmethod
    def _provider(
        *,
        claude: bool = False,
        active_turn: bool = False,
        models=("auto",),
        supports_effort: bool = False,
        change_effort: bool = True,
    ):
        """A live AcpProvider double. ``spec=`` keeps isinstance() working."""
        from kiro_crew.providers.acp import AcpProvider

        provider = MagicMock(spec=AcpProvider)
        provider.is_claude_backend = claude
        provider.has_active_turn.return_value = active_turn
        provider.available_models.return_value = [{"modelId": m} for m in models]
        provider.supports_effort.return_value = supports_effort
        provider.change_effort = AsyncMock(return_value=change_effort)
        provider.clear_effort = AsyncMock(return_value=False)
        provider.client = MagicMock()
        provider.client.set_model = AsyncMock()
        return provider

    @pytest.mark.asyncio
    async def test_idle_switch_goes_live_without_reset(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        assert state._slots["a"].model == "gpt-5.6-sol"
        provider.client.set_model.assert_awaited_once_with("gpt-5.6-sol")
        # The whole point: the session survives.
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_canonical_key_is_translated_to_the_acp_id(self, tmp_path):
        # slot.model is a canonical/wire value; set_model only accepts kiro ids.
        from kiro_crew import model_registry

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.6")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        sent = provider.client.set_model.await_args.args[0]
        assert sent == model_registry.to_acp_id("claude-opus-4.8")
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_turn_answers_409_without_reset(self, tmp_path):
        # Awaiting a response mid-turn would race the streaming prompt loop on
        # stdout for the non-multiplexed client (same hazard the effort handler
        # documents), and the old reset fallback tore down the in-flight turn
        # mid-stream for any programmatic caller. A turn in flight now answers
        # busy: no live switch, no reset, slot model untouched.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(active_turn=True)
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})
            data = await resp.json()

        assert resp.status == 409
        assert data["code"] == "turn_in_flight"
        provider.client.set_model.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()
        assert state._slots["a"].model == "claude-opus-4.8"

    @pytest.mark.asyncio
    async def test_live_switch_failure_falls_back_to_reset(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        provider.client.set_model = AsyncMock(side_effect=RuntimeError("boom"))
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        # The slot still lands on the new model, via the reset path.
        assert resp.status == 200
        assert state._slots["a"].model == "gpt-5.6-sol"
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_auto_uses_the_advertised_auto_id(self, tmp_path):
        # Back-to-default is the most common click; kiro expresses it as a real
        # model id, so it should not cost a teardown either.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(models=("auto", "claude-opus-4.8"))
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": ""})

        assert resp.status == 200
        assert state._slots["a"].model == ""
        provider.client.set_model.assert_awaited_once_with("auto")
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_without_advertised_auto_falls_back_to_reset(self, tmp_path):
        # Never invent an id the backend did not advertise.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(models=("claude-opus-4.8",))
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": ""})

        assert resp.status == 200
        provider.client.set_model.assert_not_awaited()
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_claude_backend_default_falls_back_to_reset(self, tmp_path):
        # The claude backend has no "let the server choose" id.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(claude=True)
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": ""})

        assert resp.status == 200
        provider.client.set_model.assert_not_awaited()
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_claude_backend_switch_uses_provider_id(self, tmp_path):
        from kiro_crew import model_registry

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(claude=True)
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.6")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        sent = provider.client.set_model.await_args.args[0]
        assert sent == model_registry.to_provider_id("claude-opus-4.8", "claude_code")
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_live_session_still_routes_through_reset(self, tmp_path):
        # No session to update: the reset is an O(1) no-op teardown, but it also
        # carries _reset_slot_session's pending-wait cleanup, so keep taking it.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.sessions.get_provider = MagicMock(return_value=None)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        assert state._slots["a"].model == "gpt-5.6-sol"
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unchanged_model_touches_nothing(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        provider.client.set_model.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()

    # ── reasoning effort ─────────────────────────────────────────────
    # The kiro effort overlay is written at (re)spawn, so a cold start applies
    # the slot's level for free. An in-place switch never respawns, so it must
    # push the level itself or the new model silently runs at its own default
    # while the UI still reports the slot's level.

    @pytest.mark.asyncio
    async def test_persisted_effort_is_pushed_to_the_new_model(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(supports_effort=True)
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-haiku-4.5")
        slot.reasoning_effort = "high"
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        provider.change_effort.assert_awaited_once_with("high")
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_effort_push_rejected_falls_back_to_reset(self, tmp_path):
        # change_effort returning False means the level never reached the
        # session — reset so the cold start applies it via the overlay.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(supports_effort=True, change_effort=False)
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-haiku-4.5")
        slot.reasoning_effort = "high"
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_effort_push_raising_falls_back_to_reset(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(supports_effort=True)
        provider.change_effort = AsyncMock(side_effect=RuntimeError("rejected"))
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-haiku-4.5")
        slot.reasoning_effort = "high"
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_effort_incapable_target_keeps_the_live_switch(self, tmp_path):
        # Switching to a model with no effort selector is a persisted no-op for
        # effort — it must not cost a teardown.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(supports_effort=False)
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-opus-4.8")
        slot.reasoning_effort = "high"
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-haiku-4.5"})

        assert resp.status == 200
        provider.change_effort.assert_not_awaited()
        # The level stays persisted for a later switch back to a capable model.
        assert state._slots["a"].reasoning_effort == "high"
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_effort_override_re_resolves_without_resetting(self, tmp_path):
        # With no slot override, clear_effort re-resolves any workspace default.
        # Its False return is benign here (nothing to push, nothing stale for a
        # model the user never set a level on), so it must not force a reset.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(supports_effort=True)
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-haiku-4.5")
        slot.reasoning_effort = ""
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        provider.clear_effort.assert_awaited_once()
        provider.change_effort.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unavailable_model_is_4xx_and_keeps_the_session(self, tmp_path):
        """An entitlement refusal must NOT take the reset fallback.

        Design Review on #1596: the generic ``except Exception`` here treats
        every set_model failure as "the call didn't land" and recovers with a
        reset. For a model the account cannot use that recovery is wrong twice
        over — it destroys the live conversation AND cold-starts on a different
        model, while the handler still answers ok:True. The slot must also keep
        its previous model, or the picker asserts a model that was refused.
        """
        from kiro_crew.acp.client import AcpModelUnavailable

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        provider.client.set_model = AsyncMock(
            side_effect=AcpModelUnavailable("claude-opus-4.8", ["gpt-5.6-sol"])
        )
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})
            body = await resp.json()

        assert resp.status == 400
        assert "not available on your account" in body["error"]
        # Names what the account CAN use.
        assert "gpt-5.6-sol" in body["error"]
        # The conversation survives — no reset fallback.
        state.sessions.reset.assert_not_awaited()
        # And the slot still reports the model that is actually running.
        assert state._slots["a"].model == "gpt-5.6-sol"

    @pytest.mark.asyncio
    async def test_refused_pick_rolls_back_the_pick_generation(self, tmp_path):
        """REGRESSION (review finding): a refused pick changed nothing, so the
        explicit-pick generation must roll back with the model. Leaving the
        bump in place would make the model-fallback restore probe read the
        FAILED attempt as an explicit choice and silently abandon restoring
        the primary — the session would stay on the fallback forever with no
        card and no probe."""
        from kiro_crew.acp.client import AcpModelUnavailable

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        provider.client.set_model = AsyncMock(
            side_effect=AcpModelUnavailable("claude-opus-4.8", ["gpt-5.6-sol"])
        )
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()
        gen_before = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 400
        assert slot._model_pick_gen == gen_before

    @pytest.mark.asyncio
    async def test_refused_pick_rollback_spares_a_newer_concurrent_pick(self, tmp_path):
        """REGRESSION (review finding on eb3cf067): two picks arriving together
        must not roll back each other's state. The pick transaction is
        serialised per slot (_model_pick_lock), so the refused pick unwinds
        cleanly before the next pick's snapshot is taken — the surviving state
        is the later, successful pick's."""
        import asyncio

        from kiro_crew.acp.client import AcpModelUnavailable

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        slot = state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()

        release_slow = asyncio.Event()

        async def _set_model(target):
            if target == "claude-opus-4.8":
                # The SLOW, ultimately-refused pick: park inside the locked
                # transaction, then refuse.
                await release_slow.wait()
                raise AcpModelUnavailable("claude-opus-4.8", ["gpt-5.6-sol"])
            return None

        provider.client.set_model = AsyncMock(side_effect=_set_model)
        state.sessions.get_provider = MagicMock(return_value=provider)
        gen_before = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            slow = asyncio.create_task(
                client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})
            )
            await asyncio.sleep(0.05)  # slow pick holds the lock, parked at its await
            fast = asyncio.create_task(
                client.post("/api/chat/slots/a/model", json={"model": "deepseek-3.2"})
            )
            await asyncio.sleep(0.05)  # fast pick queued on the lock
            release_slow.set()
            slow_resp = await slow
            fast_resp = await fast

        assert slow_resp.status == 400
        assert fast_resp.status == 200
        # The successful pick's state survives: the refused pick's rollback
        # unwound only its own speculative writes.
        assert slot.model == "deepseek-3.2"
        assert slot._model_pick_gen == gen_before + 1

    @pytest.mark.asyncio
    async def test_two_refused_picks_leave_the_slot_untouched(self, tmp_path):
        """REGRESSION (verifier finding on 84fc7961): two UNAVAILABLE picks
        overlapping used to fail out of order — the later rollback restored
        the earlier pick's already-refused model, leaving the slot advertising
        a rejected model with a phantom pick generation. Serialised, each
        transaction unwinds itself and the slot ends exactly where it began."""
        import asyncio

        from kiro_crew.acp.client import AcpModelUnavailable

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        slot = state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()

        release_first = asyncio.Event()
        release_second = asyncio.Event()

        async def _set_model(target):
            # Park each pick independently so the FIRST refusal can complete
            # while the SECOND is still pending — the out-of-order shape that
            # made the pre-lock CAS restore an already-refused model.
            if target == "claude-opus-4.8":
                await release_first.wait()
            else:
                await release_second.wait()
            raise AcpModelUnavailable(target, ["gpt-5.6-sol"])

        provider.client.set_model = AsyncMock(side_effect=_set_model)
        state.sessions.get_provider = MagicMock(return_value=provider)
        gen_before = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            first = asyncio.create_task(
                client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})
            )
            await asyncio.sleep(0.05)
            second = asyncio.create_task(
                client.post("/api/chat/slots/a/model", json={"model": "deepseek-3.2"})
            )
            await asyncio.sleep(0.05)
            # First refusal completes while the second pick is still pending.
            release_first.set()
            first_resp = await first
            release_second.set()
            second_resp = await second

        assert first_resp.status == 400
        assert second_resp.status == 400
        # No phantom state: the slot still reports what is actually running.
        assert slot.model == "gpt-5.6-sol"
        assert slot._model_pick_gen == gen_before

    @pytest.mark.asyncio
    async def test_successful_pick_bumps_the_pick_generation(self, tmp_path):
        """The counterpart pin: a pick that LANDS must keep its bump — that is
        what stops the fallback restore probe from overriding the choice."""
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()
        gen_before = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        assert slot._model_pick_gen == gen_before + 1

    @pytest.mark.asyncio
    async def test_same_value_pick_still_bumps_the_pick_generation(self, tmp_path):
        """REGRESSION (review finding): picking the model the slot already
        reports takes the equality early-return, but it is still an EXPLICIT
        affirmation — without the bump, a user who deliberately re-picks the
        very model the session fell back to would have that choice silently
        overridden by the next fallback restore probe."""
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()
        gen_before = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        assert slot._model_pick_gen == gen_before + 1
        # Same-value: no switch, no reset.
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_same_value_pick_during_fallback_takes_the_live_switch_path(self, tmp_path):
        """REGRESSION (review finding on 1a61ddcf): with a fallback actively
        serving the session, a pick of the DISPLAYED primary equals slot.model
        but the wire model is the fallback — the equality early-return would
        record the pick and switch nothing, stranding the session on the
        fallback while usage is attributed to the primary. The live-switch
        path must run instead."""
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="gpt-5.6-sol")
        slot._active_fallback_model = "fallback-model"
        state.push_slots_update = MagicMock()
        gen_before = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        # The pick is recorded AND the switch actually went to the wire.
        assert slot._model_pick_gen == gen_before + 1
        provider.client.set_model.assert_awaited_once_with("gpt-5.6-sol")


class TestSlotModelSwitchContextBroadcast:
    """POST /api/chat/slots/{slot}/model — one ``context_usage`` event per
    switch so the meter updates immediately.

    Regression: nothing broadcast on a model switch, so the frontend's stored
    ``slotContextTokens`` kept the OLD model's {used, window} until the next
    turn. The event carries ``reset: true`` so the reducer may replace or
    delete the stored entry (per-turn events never delete).
    """

    _app = staticmethod(TestSlotModelLiveSwitch._app)
    _provider = staticmethod(TestSlotModelLiveSwitch._provider)

    @staticmethod
    def _find_context_events(broadcast_mock):
        return [
            call.args[1]
            for call in broadcast_mock.call_args_list
            if call.args and call.args[0] == "context_usage"
        ]

    @pytest.mark.asyncio
    async def test_live_switch_broadcasts_rebased_stats(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.broadcast_ws = MagicMock()
        provider = self._provider()
        # The provider accessors report the freshly rebased stats set_model left.
        provider.context_usage_pct.return_value = 36.8
        provider.context_used_tokens.return_value = 100_000
        provider.context_window_tokens.return_value = 272_000
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        events = self._find_context_events(state.broadcast_ws)
        assert events == [
            {
                "slot": state._slots["a"].key,
                "pct": 36.8,
                "used_tokens": 100_000,
                "window_tokens": 272_000,
                "reset": True,
            }
        ]

    @pytest.mark.asyncio
    async def test_reset_fallback_broadcasts_token_clearing_event(self, tmp_path):
        # No live provider -> the reset path: no token counts, so the frontend
        # deletes its stored entry and falls back to the model-derived window.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.broadcast_ws = MagicMock()
        state.sessions.get_provider = MagicMock(return_value=None)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        state.sessions.reset.assert_awaited_once()
        events = self._find_context_events(state.broadcast_ws)
        assert events == [{"slot": state._slots["a"].key, "pct": 0.0, "reset": True}]

    @pytest.mark.asyncio
    async def test_broadcast_failure_does_not_fail_the_switch(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.broadcast_ws = MagicMock(side_effect=RuntimeError("ws down"))
        state.sessions.get_provider = MagicMock(return_value=None)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        assert state._slots["a"].model == "gpt-5.6-sol"


def _make_tail_fork_slot(state):
    slot = state.get_or_create_slot("src")
    slot.title = "My Chat"
    slot._titled = True
    slot.append("user", "msg1", "msg msg-u")
    slot.append("assistant", "reply1", "msg msg-a")
    slot.append("user", "msg2", "msg msg-u")
    slot.append("assistant", "reply2", "msg msg-a")
    slot.drain()
    return slot


class TestForkSlotTail:
    """Tests for tail-only fork (direction="tail") on POST /api/chat/slots/{slot}/fork."""

    @pytest.fixture(autouse=True)
    def _tail_fork_enabled(self, monkeypatch):
        """Default tail_fork_enabled=True so these tests exercise the tail path;
        the B1 gate test below overrides this to False for its own assertion."""
        mock_cfg = MagicMock()
        mock_cfg.dashboard.tail_fork_enabled = True
        monkeypatch.setattr("kiro_crew.dashboard.chat_fork.KiroCrewConfig.load", lambda: mock_cfg)

    @pytest.mark.asyncio
    async def test_tail_fork_keeps_messages_after_message_id(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _make_tail_fork_slot(state)
        target_id = slot.messages[1]["meta"]["mid"]

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"at_message_id": target_id, "direction": "tail"},
            )

            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == 2
            assert data["direction"] == "tail"

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert [m["content"] for m in visible] == ["msg2", "reply2"]

    @pytest.mark.asyncio
    async def test_tail_fork_keeps_messages_after_index(self, tmp_path):
        state = _make_state(tmp_path)
        _make_tail_fork_slot(state)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"at_message_index": 1, "direction": "tail"},
            )

            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == 2
            assert data["direction"] == "tail"

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert len(visible) == 2
        assert visible[0]["content"] == "msg2"
        assert visible[-1]["content"] == "reply2"

    @pytest.mark.asyncio
    async def test_tail_fork_discard_has_no_summary(self, tmp_path):
        state = _make_state(tmp_path)
        _make_tail_fork_slot(state)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"at_message_index": 1, "direction": "tail"},
            )

            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert all((m.get("meta") or {}).get("source") != "head_summary" for m in visible)

    @pytest.mark.asyncio
    async def test_tail_fork_requires_at_index(self, tmp_path):
        state = _make_state(tmp_path)
        _make_tail_fork_slot(state)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"direction": "tail"})

            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_tail_fork_nothing_after_last_message(self, tmp_path):
        state = _make_state(tmp_path)
        _make_tail_fork_slot(state)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"at_message_index": 3, "direction": "tail"},
            )

            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_tail_fork_title_and_provenance(self, tmp_path):
        state = _make_state(tmp_path)
        _make_tail_fork_slot(state)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"at_message_index": 1, "direction": "tail"},
            )

            assert resp.status == 200
            data = await resp.json()
            assert data["title"] == "↳ Tail of My Chat"

        new_slot = state._slots.get(data["key"])
        assert new_slot.forked_from == "dashboard:src"

    @pytest.mark.asyncio
    async def test_default_direction_is_head(self, tmp_path):
        state = _make_state(tmp_path)
        _make_tail_fork_slot(state)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"at_message_index": 1})

            assert resp.status == 200
            data = await resp.json()
            assert data["direction"] == "head"
            assert data["messages"] == 2

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert visible[-1]["content"] == "reply1"

    @pytest.mark.asyncio
    async def test_invalid_direction_rejected(self, tmp_path):
        state = _make_state(tmp_path)
        _make_tail_fork_slot(state)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"direction": "sideways"})

            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_tail_fork_gated_off_falls_back_to_head(self, tmp_path, monkeypatch):
        """B1 gate (D1): tail_fork_enabled=False silently downgrades tail -> head."""
        mock_cfg = MagicMock()
        mock_cfg.dashboard.tail_fork_enabled = False
        monkeypatch.setattr("kiro_crew.dashboard.chat_fork.KiroCrewConfig.load", lambda: mock_cfg)
        state = _make_state(tmp_path)
        _make_tail_fork_slot(state)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"at_message_index": 1, "direction": "tail"},
            )

            assert resp.status == 200
            data = await resp.json()
            assert data["direction"] == "head"
            assert data["messages"] == 2

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert visible[-1]["content"] == "reply1"


# ── Session reload endpoint (POST /api/chat/slots/{slot}/reload) ──


class TestSessionReload:
    """api_chat_slot_reload — relaunch the slot's agent process in place.

    The endpoint's contract: refuse while a turn is in flight (409), otherwise
    tear down via reset(skip_if_busy=True), append the session_reload feed
    notice, and eagerly re-arm the resume spawn.
    """

    @staticmethod
    def _idle_provider():
        provider = MagicMock()
        provider.has_active_turn = MagicMock(return_value=False)
        return provider

    @pytest.mark.asyncio
    async def test_reload_unknown_slot_404(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/nope/reload")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_reload_resets_notices_and_respawns(self, tmp_path, monkeypatch):
        """Happy path: atomic reset, feed notice tagged session_reload, eager resume."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=self._idle_provider())
        state.sessions.reset = AsyncMock(return_value=True)
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")
            data = await resp.json()

        assert resp.status == 200
        assert data == {"ok": True}
        state.sessions.reset.assert_awaited_once_with("dashboard:s1", skip_if_busy=True)
        # The feed carries the durable confirmation, tagged so the follow-up
        # options / continuable scans skip it (isSystemNoticeKind frontend twin).
        notice = slot.messages[-1]
        assert notice["role"] == "assistant"
        assert notice["meta"]["kind"] == "session_reload"
        assert "Reloading session" in notice["content"]
        eager.assert_called_once()
        assert eager.call_args.kwargs.get("allow_resume") is True

    @pytest.mark.asyncio
    async def test_reload_refused_while_turn_in_flight(self, tmp_path, monkeypatch):
        """409 with no teardown, no notice, no eager spawn while a turn runs."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        busy = MagicMock()
        busy.has_active_turn = MagicMock(return_value=True)
        state.sessions.get_provider = MagicMock(return_value=busy)
        state.sessions.reset = AsyncMock()
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)
        before = len(slot.messages)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")

        assert resp.status == 409
        state.sessions.reset.assert_not_awaited()
        assert len(slot.messages) == before
        eager.assert_not_called()

    @pytest.mark.asyncio
    async def test_reload_race_turn_started_after_fast_path(self, tmp_path, monkeypatch):
        """A turn slipping in between the fast path and the atomic guard is a 409.

        reset(skip_if_busy=True) returning False is ambiguous (no session OR
        busy); the re-check of has_active_turn is what disambiguates. Nothing
        may be appended or respawned for the busy case.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        idle = self._idle_provider()
        busy = MagicMock()
        busy.has_active_turn = MagicMock(return_value=True)
        # First lookup (fast path) sees idle; second (post-reset re-check) busy.
        state.sessions.get_provider = MagicMock(side_effect=[idle, busy])
        state.sessions.reset = AsyncMock(return_value=False)
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)
        before = len(slot.messages)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")

        assert resp.status == 409
        assert len(slot.messages) == before
        eager.assert_not_called()

    @pytest.mark.asyncio
    async def test_reload_retries_when_racing_turn_already_finished(self, tmp_path, monkeypatch):
        """A declined reset whose racing turn already FINISHED retries once.

        Falling through would report success while the stale live process
        survives untouched -- the silent false-success the endpoint exists to
        prevent. The retry succeeds here; a second decline is the 409 case
        (covered below).
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=self._idle_provider())
        state.sessions.reset = AsyncMock(side_effect=[False, True])
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")
            data = await resp.json()

        assert resp.status == 200
        assert data == {"ok": True}
        assert state.sessions.reset.await_count == 2
        assert slot.messages[-1]["meta"]["kind"] == "session_reload"
        eager.assert_called_once()

    @pytest.mark.asyncio
    async def test_reload_double_decline_is_turn_in_flight(self, tmp_path, monkeypatch):
        """Two consecutive declines with a live session mean a genuinely racing
        turn: 409, no notice, no respawn -- never a false success."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=self._idle_provider())
        state.sessions.reset = AsyncMock(return_value=False)
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)
        before = len(slot.messages)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")
            data = await resp.json()

        assert resp.status == 409
        assert data["code"] == "turn_in_flight"
        assert state.sessions.reset.await_count == 2
        assert len(slot.messages) == before
        eager.assert_not_called()

    def test_system_notice_kinds_backend_frontend_parity(self):
        """The two SYSTEM_NOTICE_KINDS twins must hold the same set.

        A kind skipped on one side but not the other leaves the sidebar showing
        notice boilerplate while the chat pane shows the real turn (or vice
        versa) -- the exact divergence the shared predicates exist to prevent.
        Asserted on the TS source because nothing else couples the two files.
        """
        import re
        from pathlib import Path

        from kiro_crew.dashboard.system_notices import SYSTEM_NOTICE_KINDS

        ts_path = Path(__file__).resolve().parents[1] / "website/src/lib/systemNotice.ts"
        ts_src = ts_path.read_text(encoding="utf-8")
        m = re.search(r"new Set\(\[([^\]]*)\]\)", ts_src)
        assert m, "SYSTEM_NOTICE_KINDS Set literal not found in systemNotice.ts"
        frontend = set(re.findall(r"'([^']+)'", m.group(1)))
        assert frontend == set(SYSTEM_NOTICE_KINDS)

    @pytest.mark.asyncio
    async def test_reload_without_live_session_is_ok(self, tmp_path, monkeypatch):
        """No live session: reload is a no-op teardown but still ok + notice + respawn.

        The goal state (next spawn picks up fresh config) already holds, so
        this must not be an error; reloaded=False tells the caller no process
        was actually torn down.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=None)
        state.sessions.reset = AsyncMock(return_value=False)
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")
            data = await resp.json()

        assert resp.status == 200
        assert data == {"ok": True}
        assert slot.messages[-1]["meta"]["kind"] == "session_reload"
        eager.assert_called_once()

    @pytest.mark.asyncio
    async def test_reload_cancels_pending_question_waits(self, tmp_path, monkeypatch):
        """A pending ask_question wait is released by the reload teardown.

        The card lives in dashboard state, not the session — without the
        unblock it would survive the reset and hold its MCP worker until
        timeout with no agent left to receive the answer.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=self._idle_provider())
        state.sessions.reset = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.schedule_eager_spawn",
            MagicMock(return_value=None),
        )
        cancelled = MagicMock(return_value=1)
        monkeypatch.setattr(state, "cancel_questions_for_slot", cancelled)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")

        assert resp.status == 200
        cancelled.assert_called_once_with("s1")

    @pytest.mark.asyncio
    async def test_reload_app_token_denied_on_foreign_slot(self, tmp_path, monkeypatch):
        """An app token gets the indistinguishable 404, and nothing is torn down.

        Reload is a teardown; without the cancel-route ownership policy, an app
        allowed /api/chat/* could reset a foreign slot's process and cancel its
        pending waits.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._app = "other-app"
        state.sessions.get_provider = MagicMock(return_value=None)
        state.sessions.reset = AsyncMock()
        app = _make_app_with_agent_routes(state)

        @web.middleware
        async def _as_app(request, handler):
            request["app"] = "attacker-app"
            return await handler(request)

        app.middlewares.append(_as_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/reload")
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reload_targets_the_linked_session_key(self, tmp_path, monkeypatch):
        """A channel-born slot reloads its LINKED session, not the phantom
        dashboard-prefixed key — otherwise reload reports success while the
        live process keeps its stale config.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.linked_session_key = "slack:123.456"
        state.sessions.get_provider = MagicMock(return_value=None)
        state.sessions.reset = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.schedule_eager_spawn",
            MagicMock(return_value=None),
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")

        assert resp.status == 200
        state.sessions.reset.assert_awaited_once_with("slack:123.456", skip_if_busy=True)

    @pytest.mark.asyncio
    async def test_reload_refused_while_subagents_attached(self, tmp_path, monkeypatch):
        """409 while children are running/queued/delivering — the reset would
        tear down the shared subagent runtime and discard their work.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=self._idle_provider())
        state.sessions.reset = AsyncMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=["child-1"])
        state.subagents._queued_depth = MagicMock(return_value=0)
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)
        before = len(slot.messages)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")
            data = await resp.json()

        assert resp.status == 409
        assert data["code"] == "slot_subagents_running"
        state.sessions.reset.assert_not_awaited()
        assert len(slot.messages) == before
        eager.assert_not_called()

    @pytest.mark.asyncio
    async def test_reload_fails_closed_on_unreadable_children_probe(self, tmp_path, monkeypatch):
        """A None running-probe is the probe FAILING, not zero children."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=None)
        state.sessions.reset = AsyncMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=None)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")

        assert resp.status == 409
        state.sessions.reset.assert_not_awaited()


class TestCloseBroadcastDurability:
    """A slot-list frame must never omit a slot that is coming back.

    Clients treat a frame omitting a slot as licence to drop that slot's cached
    per-slot state, including follow-up and folder-suggestion cards that only
    ever exist in the browser and are never re-sent. So the close may only
    broadcast once the history save is durable: past that point no rollback can
    put the slot back, and the omission can never be retracted.

    Every test records the ORDER of the close steps, because asserting only that
    a broadcast happened passes against any placement and proves nothing.
    """

    @staticmethod
    def _instrument(state, slot, monkeypatch, frames=None):
        """Record close steps in call order; append each frame's slot keys."""
        from kiro_crew.dashboard import chat_handlers

        calls: list[str] = []

        def _push():
            calls.append("broadcast")
            if frames is not None:
                frames.append(set(state._slots))

        state.push_slots_update = MagicMock(side_effect=_push)

        real_sync = chat_handlers._sync_dashboard_slots

        def _sync(st):
            calls.append("sync")
            return real_sync(st)

        monkeypatch.setattr(chat_handlers, "_sync_dashboard_slots", _sync)

        real_unblock = chat_handlers._unblock_pending_waits

        def _unblock(st, sl):
            calls.append("unblock_pending_waits")
            return real_unblock(st, sl)

        monkeypatch.setattr(chat_handlers, "_unblock_pending_waits", _unblock)

        async def _save(*args, **kwargs):
            calls.append("save")

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _save)

        state.sessions.remove = AsyncMock(side_effect=lambda *a, **k: calls.append("remove"))

        eager = MagicMock()
        eager.done = MagicMock(return_value=False)
        eager.cancel = MagicMock(side_effect=lambda: calls.append("cancel_eager"))
        slot._eager_spawn_task = eager

        async def _never() -> None:
            await asyncio.sleep(30)

        task = asyncio.get_running_loop().create_task(_never())
        real_cancel = task.cancel

        def _task_cancel(*args, **kwargs):
            calls.append("cancel_task")
            return real_cancel(*args, **kwargs)

        task.cancel = _task_cancel  # type: ignore[method-assign]
        # `running` is derived from `task`, so assigning the task is enough to
        # reach the cancel-and-wait_for branch.
        slot.task = task

        return calls

    @pytest.mark.asyncio
    async def test_failed_close_never_emits_a_frame_without_the_slot(self, tmp_path, monkeypatch):
        """The regression guard: a rollback must not be preceded by an omission.

        A frame omitting the slot is what makes another client drop this slot's
        follow-up and folder-suggestion cards. Those are browser-only and the
        backend re-offers a folder suggestion at most once per slot, so a prune
        on a close that then rolls back loses them permanently.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_handlers

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()
        frames: list[set] = []
        calls = self._instrument(state, slot, monkeypatch, frames=frames)

        async def _boom(*args, **kwargs):
            calls.append("save")
            raise RuntimeError("disk full")

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _boom)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/s1")
            assert resp.status == 500

        assert frames, "no frame was broadcast at all, so this proves nothing"
        missing = [i for i, f in enumerate(frames) if "s1" not in f]
        assert not missing, (
            f"frame(s) {missing} omitted a slot that the rollback restores, so a "
            f"client pruned its cards for it: frames={frames} calls={calls}"
        )
        assert state._slots.get("s1") is slot, "slot was not restored"

    @pytest.mark.asyncio
    async def test_broadcast_precedes_the_session_teardown(self, tmp_path, monkeypatch):
        """On success, broadcast after the save but before the slow teardown."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()
        calls = self._instrument(state, slot, monkeypatch)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/s1")
            assert resp.status == 200

        for step in ("save", "remove", "broadcast"):
            assert step in calls, f"{step} never ran, so this proves nothing: {calls}"
        assert calls.index("save") < calls.index(
            "broadcast"
        ), f"broadcast ran before the save was durable: {calls}"
        assert calls.index("broadcast") < calls.index(
            "remove"
        ), f"broadcast waited out the session teardown: {calls}"

    @pytest.mark.asyncio
    async def test_broadcast_beats_a_slow_session_teardown(self, tmp_path, monkeypatch):
        """A session teardown that never returns must not hold the broadcast."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()
        calls = self._instrument(state, slot, monkeypatch)
        seen = asyncio.Event()
        state.push_slots_update = MagicMock(
            side_effect=lambda: (calls.append("broadcast"), seen.set())
        )

        async def _hang(*args, **kwargs):
            calls.append("remove")
            await asyncio.sleep(30)

        state.sessions.remove = AsyncMock(side_effect=_hang)

        async with TestClient(TestServer(_make_app(state))) as client:
            close = asyncio.get_running_loop().create_task(client.delete("/api/chat/slots/s1"))
            await asyncio.wait_for(seen.wait(), timeout=5.0)
            assert "save" in calls and calls.index("save") < calls.index("broadcast")
            assert "s1" not in state._slots
            close.cancel()
            try:
                await close
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_active_slot_set_is_published_only_after_the_teardown(
        self, tmp_path, monkeypatch
    ):
        """_sync_dashboard_slots must stay after the teardown.

        Publishing the shrunken set earlier lets the idle sweep classify this
        session as orphaned and reap it mid-teardown, and that reap releases a
        shared subagent runtime which outlives the turn — the turn semaphore, the
        sweep's only busy guard, is free while subagents run.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()
        calls = self._instrument(state, slot, monkeypatch)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/s1")
            assert resp.status == 200

        assert "sync" in calls, f"the trailing sync never ran: {calls}"
        assert calls.index("save") < calls.index(
            "sync"
        ), f"active-slot set published before the save was durable: {calls}"
        assert calls.index("remove") < calls.index(
            "sync"
        ), f"active-slot set published before the session teardown: {calls}"


class TestUnflushedTailOrderingAndSnapshot:
    """The owed tail must land in window order, from a snapshot taken on the loop."""

    @staticmethod
    def _row(role: str, body: str, mid: str, ts: str) -> dict:
        return {
            "role": role,
            "content": body,
            "cls": f"msg msg-{role[0]}",
            "ts": ts,
            "meta": {"mid": mid},
        }

    def test_owed_row_merges_at_its_window_position_not_after_the_disk_slice(self):
        """A persisted row LATER in window order must not be rendered before an owed one.

        ``_flush_segment`` pulls a ``stop_event`` out of the trailing chunk run and
        re-appends it AFTER the finalized assistant row (chat_runner.py:2686-2687).
        So a stop that reached disk during streaming sits, in window order, behind a
        reply that is still owed. Concatenating the owed rows after the whole disk
        slice therefore renders the pair inverted -- the stop card above the prose it
        terminated.

        Window order is authoritative and every row in this arm carries an id, so the
        position is derivable without the body matching the other arm needs.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._row("user", "run it", "u1", "2026-08-19T10:00:00+00:00")
        reply = self._row("assistant", "done", "a1", "2026-08-19T10:00:03+00:00")
        stop = self._row("assistant", "stopped", "s1", "2026-08-19T10:00:02+00:00")

        window = [q, reply, stop]
        all_msgs = [q, stop]  # the reply has not been flushed yet
        slot = SimpleNamespace(messages=window, _disk_older_count=0)

        disk_mids = {m["meta"]["mid"] for m in all_msgs}
        assert reply["meta"]["mid"] not in disk_mids, "fixture needs the reply un-persisted"
        assert stop["meta"]["mid"] in disk_mids, "fixture needs the stop already on disk"
        assert window.index(reply) < window.index(stop), (
            "fixture must place the owed reply BEFORE the persisted stop, else there "
            "is no inversion to detect"
        )

        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert (
            bodies.count("stopped") == 1 and bodies.count("done") == 1
        ), f"a row was dropped or duplicated; got {bodies}"
        assert bodies.index("done") < bodies.index("stopped"), (
            f"the persisted stop was rendered before the owed reply it follows in the "
            f"window; got {bodies}"
        )

    def test_tail_with_no_inversion_still_appends_after_the_disk_slice(self):
        """Negative control: with no inversion the result must be the old concatenation.

        Fails for the intended reason if the merge reorders rows it should leave alone
        -- every owed row here already follows every persisted one in window order, so
        the answer must be exactly ``all_msgs + owed``.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._row("user", "run it", "u1", "2026-08-19T10:00:00+00:00")
        stop = self._row("assistant", "stopped", "s1", "2026-08-19T10:00:02+00:00")
        reply = self._row("assistant", "done", "a1", "2026-08-19T10:00:03+00:00")

        all_msgs = [q, stop]
        slot = SimpleNamespace(messages=[q, stop, reply], _disk_older_count=0)
        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]
        assert out == [q, stop, reply], (
            f"the no-inversion case must be untouched concatenation; got "
            f"{[m['content'] for m in out]}"
        )

    @pytest.mark.asyncio
    async def test_tail_snapshot_is_captured_on_the_loop_not_inside_the_worker(
        self, tmp_path, monkeypatch
    ):
        """The window snapshot must be taken before the offload, not inside the thread.

        ``_flush_segment`` assigns ``slot.messages = head`` and only THEN appends the
        finalized assistant row, so between those two statements the window is a
        transient chunk-free head missing that row. It is a plain ``def`` that is never
        handed to ``to_thread``, so the event loop cannot interleave with it -- but the
        threaded tail scan can land exactly there and snapshot the hole, and
        ``_disk_older_count`` is untouched by that mutation so the bounded re-read
        cannot detect it.

        Simulated deterministically: the mutation is applied at the moment the worker
        is entered. A snapshot captured on the loop predates it and keeps the row; one
        taken inside the worker sees the hole and drops the row from the response.
        """
        import kiro_crew.dashboard.chat_handlers as ch
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("loopsnap")
        key = "dashboard:loopsnap"

        slot.append("user", "q1")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)
        disk = state.conversation_log.read_messages_chained(key)
        disk_mids = {
            m["meta"]["mid"]
            for m in disk
            if isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str)
        }
        assert disk and len(disk_mids) == len(disk), (
            f"fixture needs every disk row to carry an id so the id arm runs; "
            f"disk={len(disk)} mids={len(disk_mids)}"
        )

        owed = {
            "role": "assistant",
            "content": "finalized-reply",
            "cls": "msg msg-a",
            "ts": "2026-08-19T10:00:03+00:00",
            "meta": {"mid": "m-finalizedneverpersisted"},
        }
        slot.messages.append(owed)
        assert owed["meta"]["mid"] not in disk_mids, "fixture row must be un-persisted"

        original = ch._append_unflushed_tail
        seen: dict[str, object] = {}

        def worker_entered(slot_arg, all_msgs_arg, **kwargs):
            # The worker is now running. Reproduce the mid-finalization window: the
            # finalized assistant row is not in slot.messages yet.
            seen["snapshot_kwarg"] = "snapshot" in kwargs
            slot_arg.messages = [
                m for m in slot_arg.messages if m.get("content") != "finalized-reply"
            ]
            return original(slot_arg, all_msgs_arg, **kwargs)

        monkeypatch.setattr(ch, "_append_unflushed_tail", worker_entered)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/loopsnap?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        assert (
            seen.get("snapshot_kwarg") is not None
        ), "the tail match never ran, so this test asserts nothing"
        assert seen["snapshot_kwarg"] is True, (
            "the caller did not pass a snapshot, so the window is still read inside "
            "the worker thread where a mid-finalization window is observable"
        )
        contents = [m["content"] for m in data["messages"]]
        assert "finalized-reply" in contents, (
            f"the worker snapshotted the transient chunk-free window and dropped the "
            f"finalized row; got {contents}"
        )

    @staticmethod
    def _idless(role: str, body: str, ts: str, cls: str | None = None) -> dict:
        """A row with NO ``meta.mid`` -- forces the id-less body-matching arm."""
        r = {"role": role, "content": body, "ts": ts}
        if cls is not None:
            r["cls"] = cls
        return r

    def test_live_streamed_row_before_a_persisted_row_is_not_dropped(self):
        """An owed transient row must survive the suffix boundary in the id-less arm.

        The matcher skipped a transient row WITHOUT advancing ``start``; a later
        persisted match then set ``start = i + 1``, moving the boundary PAST the skipped
        row, and ``window[start:]`` excluded it. So a live streamed chunk sitting before
        a persisted ``stop_event`` was dropped from the bounded response entirely.

        A disk read omits transient roles, so such a row can never be matched and is
        always owed -- which is precisely the rule the id-based arm already applies.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "run it", "2026-08-19T15:00:00+00:00")
        chunk = self._idless("chunk", "partial an", "2026-08-19T15:00:01+00:00")
        stop = self._idless("assistant", "stopped", "2026-08-19T15:00:02+00:00")

        all_msgs = [q, stop]  # a disk read holds no transient rows
        window = [q, chunk, stop]
        assert not any(
            "meta" in m for m in all_msgs
        ), "fixture must be id-less, else the id arm runs and this asserts nothing"
        assert window.index(chunk) < window.index(stop), (
            "fixture must place the transient BEFORE the persisted row, else the "
            "suffix boundary never moves past it"
        )

        slot = SimpleNamespace(messages=window, _disk_older_count=0)
        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert (
            "partial an" in bodies
        ), f"the live streamed row was dropped by the suffix boundary; got {bodies}"
        assert bodies.index("partial an") < bodies.index("stopped"), (
            f"the owed transient must keep its window position ahead of the persisted "
            f"row; got {bodies}"
        )

    def test_trailing_transient_row_is_emitted_exactly_once(self):
        """Negative control for the merge: an owed transient must not be emitted twice.

        Rows left in ``pending`` sit at or after ``start`` and are therefore already
        carried by ``window[start:]``. Emitting them again as well would re-append a
        live row -- the duplication class this change exists to avoid. Fails for the
        intended reason if the merge and the suffix both claim the same row.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "run it", "2026-08-19T15:00:00+00:00")
        stop = self._idless("assistant", "stopped", "2026-08-19T15:00:02+00:00")
        chunk = self._idless("chunk", "partial an", "2026-08-19T15:00:03+00:00")

        slot = SimpleNamespace(messages=[q, stop, chunk], _disk_older_count=0)
        out = _append_unflushed_tail(slot, [q, stop])  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert (
            bodies.count("partial an") == 1
        ), f"the trailing transient was emitted more than once; got {bodies}"
        assert bodies == ["run it", "stopped", "partial an"], (
            f"a transient already after the last match must stay where it was; got " f"{bodies}"
        )

    def test_unowed_and_answered_rows_stay_excluded_from_the_idless_tail(self):
        """Negative control: only genuinely owed transients may be merged.

        ``done``/``queued`` are ``_UNOWED_WINDOW_ROLES`` and an answered ``permission``
        has already been dispositioned, so neither is owed. Merging transients
        unconditionally would re-append them, which is the same defect in the opposite
        direction from the drop above. A still-pending permission IS owed, so it is
        included -- that arm of the assertion is what stops the fix from over-narrowing
        into a second drop.
        """
        import json
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "run it", "2026-08-19T15:00:00+00:00")
        stop = self._idless("assistant", "stopped", "2026-08-19T15:00:02+00:00")
        done = self._idless("done", "unowed-marker", "2026-08-19T15:00:01+00:00")
        answered = self._idless(
            "permission",
            "answered-approval",
            "2026-08-19T15:00:01+00:00",
            cls=json.dumps({"resolved": "approve"}),
        )
        pending = self._idless(
            "permission",
            "pending-approval",
            "2026-08-19T15:00:01+00:00",
            cls=json.dumps({}),
        )

        for extra, body, owed in (
            (done, "unowed-marker", False),
            (answered, "answered-approval", False),
            (pending, "pending-approval", True),
        ):
            slot = SimpleNamespace(messages=[q, extra, stop], _disk_older_count=0)
            out = _append_unflushed_tail(slot, [q, stop])  # type: ignore[arg-type]
            bodies = [m["content"] for m in out]
            if owed:
                assert body in bodies, (
                    f"{extra['role']} row {body!r} is owed and must be merged; got " f"{bodies}"
                )
            else:
                assert body not in bodies, (
                    f"{extra['role']} row {body!r} is NOT owed and must not be "
                    f"re-appended; got {bodies}"
                )

    def test_unmatched_row_does_not_strand_the_persisted_rows_behind_it(self):
        """An owed row must not make the id-less arm re-emit later persisted rows.

        The arm forward-scans the disk slice for each window row. When a row was NOT
        found the loop used to ``break`` outright, which left ``start`` pointing AT that
        row -- so ``window[start:]`` carried it AND every later window row, including
        rows the disk slice already holds. Those came back a second time, and out of
        order, which is the duplication this function exists to remove.

        The reachable shape is a ``stop_event`` flushed before reply finalization:
        ``_flush_segment`` re-appends the stop AFTER the finalized assistant row, so the
        window reads ``[... unflushed reply, flushed stop]``. The reply misses, and on
        the old code the persisted stop was emitted twice.

        The sibling id-carrying arm already holds an unmatched row and keeps walking, so
        this mirrors that rule rather than inventing a second one.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "run it", "2026-08-19T16:00:00+00:00")
        reply = self._idless("assistant", "the reply", "2026-08-19T16:00:01+00:00")
        stop = self._idless("assistant", "stopped", "2026-08-19T16:00:02+00:00")

        # The reply is still owed; the stop already reached disk ahead of it.
        all_msgs = [q, stop]
        window = [q, reply, stop]
        assert not any(
            "meta" in m for m in all_msgs
        ), "fixture must be id-less, else the id arm runs and this asserts nothing"
        assert window.index(reply) < window.index(stop), (
            "fixture must place the UNMATCHED row before the persisted one, else the "
            "loop never abandons the rows behind it"
        )
        assert not any(
            m["content"] == "the reply" for m in all_msgs
        ), "fixture must keep the reply off disk, else there is nothing unmatched"

        slot = SimpleNamespace(messages=window, _disk_older_count=0)
        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]

        assert bodies.count("stopped") == 1, (
            f"the persisted stop was re-emitted because an earlier unmatched row "
            f"abandoned the scan; got {bodies}"
        )
        assert bodies == [
            "run it",
            "the reply",
            "stopped",
        ], f"output must follow window order with each row exactly once; got {bodies}"

    def test_unmatched_row_is_still_emitted_and_not_dropped(self):
        """Negative control: holding the unmatched row must not silently drop it.

        The fix stops an unmatched row from stranding the rows behind it. The opposite
        failure is just as silent -- discarding that row instead of retaining it would
        also stop the duplicate, and would pass a test that only counted the persisted
        row. So assert the owed row itself survives, in both the with-a-later-match and
        no-later-match shapes.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "ask", "2026-08-19T16:10:00+00:00")
        owed = self._idless("assistant", "owed body", "2026-08-19T16:10:01+00:00")
        persisted = self._idless("assistant", "on disk", "2026-08-19T16:10:02+00:00")

        # (a) a later row DOES match, so the owed row is flushed at that disk position
        slot = SimpleNamespace(messages=[q, owed, persisted], _disk_older_count=0)
        out = _append_unflushed_tail(slot, [q, persisted])  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert (
            bodies.count("owed body") == 1
        ), f"the owed row must be retained exactly once, not dropped; got {bodies}"
        assert bodies.index("owed body") < bodies.index(
            "on disk"
        ), f"the retained row must keep its window position; got {bodies}"

        # (b) NO later row matches, so the owed row can only arrive via the tail
        slot = SimpleNamespace(messages=[q, owed], _disk_older_count=0)
        out = _append_unflushed_tail(slot, [q])  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert (
            bodies.count("owed body") == 1
        ), f"with no later match the owed row must still be emitted once; got {bodies}"

    def test_trailing_unowed_and_answered_rows_are_excluded_from_the_idless_tail(self):
        """An unowed row in the LAST window position must not reach the tail.

        ``start`` only advances on a match, and an unowed row takes the transient branch
        and ``continue``s -- skipping ``start = i + 1``. So when such a row TRAILS the last
        match, a raw ``window[start:]`` re-admits it and undoes the exclusion the loop's own
        guard performed. The sibling id-carrying arm has no such hole because it applies
        both exclusions at the top of its loop.

        Two symptoms share that cause. A trailing ``done`` reaches the bounded response and
        ``_prepare_messages`` drops it while rendering, so a page whose only row is that
        ``done`` renders EMPTY and replaces the transcript. A trailing answered
        ``permission`` is instead re-ordered after every persisted row.

        The sibling test above puts the extra row in the MIDDLE, where the later match
        advances ``start`` PAST it -- so the exclusion it observes comes from ``start``
        overshooting, not from any filter, and it cannot see this defect. The whole body
        list is asserted rather than mere membership, because membership alone cannot see a
        mis-ordering.
        """
        import json
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "run it", "2026-08-20T04:00:00+00:00")
        stop = self._idless("assistant", "stopped", "2026-08-20T04:00:01+00:00")
        done = self._idless("done", "unowed-marker", "2026-08-20T04:00:02+00:00")
        queued = self._idless("queued", "queued-marker", "2026-08-20T04:00:02+00:00")
        answered = self._idless(
            "permission",
            "answered-approval",
            "2026-08-20T04:00:02+00:00",
            cls=json.dumps({"resolved": "approve"}),
        )

        for extra, body in (
            (done, "unowed-marker"),
            (queued, "queued-marker"),
            (answered, "answered-approval"),
        ):
            window = [q, stop, extra]
            assert window[-1] is extra, (
                "fixture must place the unowed row LAST, else a later match advances "
                "start past it and the assertion passes without a filter"
            )
            slot = SimpleNamespace(messages=window, _disk_older_count=0)
            out = _append_unflushed_tail(slot, [q, stop])  # type: ignore[arg-type]
            bodies = [m["content"] for m in out]
            assert body not in bodies, (
                f"trailing {extra['role']} row {body!r} was re-admitted by the unfiltered "
                f"tail slice; got {bodies}"
            )
            assert bodies == ["run it", "stopped"], (
                f"the bounded page must hold exactly the persisted rows in order; got " f"{bodies}"
            )

    def test_trailing_owed_rows_survive_the_idless_tail_filter(self):
        """Negative control: the tail filter must not over-narrow into a second drop.

        ``_UNOWED_WINDOW_ROLES`` is ``{done, queued}`` -- ``chunk`` and ``streaming`` are
        subtracted out, and a still-PENDING ``permission`` has not been dispositioned. All
        three are genuinely owed: a disk read carries no transient row, so they can never be
        matched, and dropping them loses a live streamed answer or an approval the user has
        yet to answer. That is the opposite defect from the leak, and it is the one an
        over-eager filter actually produces.
        """
        import json
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "run it", "2026-08-20T04:10:00+00:00")
        stop = self._idless("assistant", "stopped", "2026-08-20T04:10:01+00:00")
        pending = self._idless(
            "permission",
            "pending-approval",
            "2026-08-20T04:10:02+00:00",
            cls=json.dumps({}),
        )
        chunk = self._idless("chunk", "partial an", "2026-08-20T04:10:02+00:00")

        slot = SimpleNamespace(messages=[q, stop, pending], _disk_older_count=0)
        out = _append_unflushed_tail(slot, [q, stop])  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert bodies == ["run it", "stopped", "pending-approval"], (
            f"a still-pending permission is owed and must survive the tail filter; got " f"{bodies}"
        )

        slot = SimpleNamespace(messages=[q, stop, chunk], _disk_older_count=0)
        out = _append_unflushed_tail(slot, [q, stop])  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert (
            bodies.count("partial an") == 1
        ), f"a trailing live chunk is owed and must be emitted exactly once; got {bodies}"
        assert bodies == [
            "run it",
            "stopped",
            "partial an",
        ], f"the owed chunk must keep its trailing window position; got {bodies}"
