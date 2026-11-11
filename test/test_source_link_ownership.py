"""A session card's GitHub chips are the pull requests and issues the chat OWNS.

Before this, any GitHub URL in any row's text became a chip, while the real
signals were ignored: the URL ``gh pr create`` printed (it sits in the tool row's
``meta.output``), the PR a worker reported to the work ledger, and the PRs a
conductor's workers opened. These tests pin the rule the card follows now:

* created -- a successful ``gh pr create`` / ``gh issue create`` row;
* acted on -- a successful ``gh pr <write verb>`` or non-GET ``gh api .../pulls/N``;
* reported -- the chat's own work-ledger item's ``pr`` or artifact URL;
* rolled up -- one level: what a worker bound to this conductor's items created
  or reported.

A URL that only appears in row text (user, assistant, nudge, a compaction
summary) makes no GitHub chip.
"""

from __future__ import annotations

import json

import pytest
import work_ledger_helpers  # noqa: F401 -- the shared data-home pin
from chat_test_helpers import _make_state
from source_link_rows import append_created, gh_create_meta

from kiro_crew import work_ledger as wl
from kiro_crew.dashboard.source_providers import ownership
from kiro_crew.dashboard.state import _ChatSlot

_isolated_home = work_ledger_helpers._isolated_home

PR = "https://github.com/kirodotdev/KiroCrew/pull/34732"
OLD = "https://github.com/kirodotdev/KiroCrew/pull/21991"
NEW = "https://github.com/kirodotdev/KiroCrew/pull/34742"


@pytest.fixture(autouse=True)
def _fresh_ledger_cache():
    ownership.reset_ledger_cache()
    yield
    ownership.reset_ledger_cache()


@pytest.fixture(autouse=True)
def _hosts_loaded(_floor_monkeypatch):
    """The host settings have loaded, so a host-less ``owner/repo`` reads as github.com."""
    from kiro_crew.dashboard.source_providers import hosts

    _floor_monkeypatch.setattr(hosts, "_gitlab_hosts_loaded_at", 1.0)


def _urls(slot: _ChatSlot) -> list[str]:
    return [link["url"] for link in slot._pr_source_links()]


def _tool(slot: _ChatSlot, command: str, output: str, ts: str = "") -> None:
    """A shell tool row as the dashboard stores it: JSON input, output with the trailer."""
    meta = {"input": json.dumps({"command": command}), "kind": "execute", "output": output}
    slot.append("tool", "🔧 Run a command", ts=ts, meta=meta)


def _ok(text: str) -> str:
    return f"Output:\n{text}\n\n\nExit Code: 0"


class TestCreated:
    def test_a_gh_pr_create_row_with_the_url_only_in_its_output_is_a_chip(self):
        # chat-539 row 379, verbatim apart from the body-file path.
        slot = _ChatSlot("chat-539")
        slot.append(
            "tool",
            "🔧 Open the PR ready for review with screenshots",
            ts="2026-11-10T18:39:20.397178+00:00",
            meta={
                "tool_call_id": "run_command_toolu_1",
                "input": json.dumps(
                    {
                        "command": "gh pr create -R kirodotdev/KiroCrew --base main "
                        '--head feat/app-sdk-issue-components --title "feat(app-sdk): x" '
                        "--body-file /tmp/prepare-pr-body.md --attach",
                        "description": "Open the PR",
                    }
                ),
                "kind": "execute",
                "done": True,
                "output": f"Output:\n{PR}\n\n\nExit Code: 0",
            },
        )
        assert _urls(slot) == [PR]
        assert slot.to_dict()["source_links_total"] == 1

    def test_the_gh_proxy_row_shape_counts_the_same(self):
        slot = _ChatSlot("remote")
        _tool(
            slot,
            "gh pr create --fill",
            _ok(f"{PR}\ngh-proxy: ran on the gateway machine as @kyle for kirodotdev/KiroCrew"),
        )
        assert _urls(slot) == [PR]

    def test_a_failed_create_is_not_a_chip(self):
        slot = _ChatSlot("s1")
        _tool(
            slot,
            "gh pr create --fill",
            'Output:\na pull request for branch "x" into branch "main" already exists:\n'
            f"{PR}\n\n\nExit Code: 1",
        )
        assert _urls(slot) == []

    def test_a_create_without_an_exit_trailer_that_says_already_exists_is_not_a_chip(self):
        slot = _ChatSlot("s1")
        _tool(slot, "gh pr create --fill", f"a pull request for branch already exists:\n{PR}\n")
        assert _urls(slot) == []

    def test_gh_issue_create_is_an_issue_chip(self):
        slot = _ChatSlot("s1")
        issue = "https://github.com/kirodotdev/KiroCrew/issues/35000"
        _tool(slot, "gh issue create --title t --body b", _ok(issue))
        assert [(link["url"], link["kind"]) for link in slot._pr_source_links()] == [
            (issue, "issue")
        ]

    def test_another_repository_and_a_fork_pr_count_too(self):
        slot = _ChatSlot("s1")
        other = "https://github.com/kiro-team/kiro-agent/pull/3915"
        _tool(slot, "gh pr create -R kiro-team/kiro-agent --fill", _ok(other), ts="t1")
        # A fork PR's URL names the base repository, like any other PR.
        fork = "https://github.com/kirodotdev/KiroCrew/pull/29466"
        _tool(slot, "gh pr create -R kirodotdev/KiroCrew --head cabbey:fix --fill", _ok(fork))
        assert set(_urls(slot)) == {other, fork}


class TestActedOn:
    @pytest.mark.parametrize("verb", ["edit", "comment", "close", "merge", "ready", "review"])
    def test_a_write_verb_with_a_repo_and_number_is_a_chip(self, verb):
        slot = _ChatSlot("s1")
        _tool(slot, f"gh pr {verb} 21627 -R kirodotdev/KiroCrew --body x", _ok("done"))
        assert _urls(slot) == ["https://github.com/kirodotdev/KiroCrew/pull/21627"]

    def test_a_bare_number_is_resolved_from_what_gh_printed(self):
        slot = _ChatSlot("s1")
        _tool(
            slot,
            "cd /repo && gh pr close 21627 --comment superseded",
            _ok("✓ Closed pull request kirodotdev/KiroCrew#21627 (old title)"),
        )
        assert _urls(slot) == ["https://github.com/kirodotdev/KiroCrew/pull/21627"]

    def test_a_url_target_is_the_chip_and_a_url_in_the_body_is_not(self):
        slot = _ChatSlot("s1")
        _tool(
            slot,
            f'gh pr comment {PR} --body "see https://github.com/acme/w/pull/9"',
            _ok(f"{PR}#issuecomment-1"),
        )
        assert _urls(slot) == [PR]

    def test_gh_pr_view_and_other_reads_are_not_chips(self):
        slot = _ChatSlot("s1")
        _tool(slot, "gh pr view 34732 -R kirodotdev/KiroCrew --json url", _ok(f'{{"url":"{PR}"}}'))
        _tool(slot, f"gh pr checks {PR}", _ok(PR))
        _tool(slot, "gh api repos/kirodotdev/KiroCrew/pulls/34732", _ok("{}"))
        assert _urls(slot) == []

    def test_a_writing_gh_api_call_on_a_pull_is_a_chip(self):
        slot = _ChatSlot("s1")
        _tool(slot, "gh api repos/kirodotdev/KiroCrew/pulls/34732/comments -f body=hi", _ok("{}"))
        _tool(slot, "gh api -X PATCH /repos/acme/w/pulls/5", _ok("{}"))
        assert set(_urls(slot)) == {PR, "https://github.com/acme/w/pull/5"}

    def test_a_failed_write_is_not_a_chip(self):
        slot = _ChatSlot("s1")
        _tool(
            slot,
            "gh pr comment 99999 -R kirodotdev/KiroCrew -b x",
            "Output:\nGraphQL: Could not resolve to a PullRequest\n\n\nExit Code: 1",
        )
        assert _urls(slot) == []


class TestMentionsAreNotChips:
    @pytest.mark.parametrize("role", ["user", "assistant", "nudge", "system"])
    def test_a_row_that_only_mentions_a_pr_makes_no_chip(self, role):
        slot = _ChatSlot("s1")
        slot.append(role, f"main is red because of {PR}", ts="t1")
        assert _urls(slot) == []
        assert slot.to_dict()["source_links_total"] == 0

    def test_a_compaction_summary_does_not_move_an_old_pr_ahead_of_a_newer_one(self):
        slot = _ChatSlot("s1")
        append_created(slot, OLD, ts="2026-11-09T10:00:00+00:00")
        append_created(slot, NEW, ts="2026-11-10T10:00:00+00:00")
        slot.append(
            "assistant",
            f"✅ Conversation compacted: ## Overview\nStill tracking {OLD}.",
            ts="2026-11-10T17:19:00+00:00",
        )
        assert _urls(slot) == [NEW, OLD]

    def test_other_providers_still_chip_from_a_mention(self):
        slot = _ChatSlot("s1")
        slot.append("user", "Please look at https://acme.atlassian.net/browse/PROJ-123", ts="t1")
        assert _urls(slot) == ["https://acme.atlassian.net/browse/PROJ-123"]

    def test_an_owned_url_only_in_meta_still_authorizes_its_unlink(self):
        slot = _ChatSlot("s1")
        append_created(slot, PR)
        (chip,) = slot._pr_source_links()
        assert slot.mentions_source_identity(chip["identity"]) is True


def _dispatch(conductor: str, worker: str, *, repo: str = "kirodotdev/KiroCrew") -> str:
    wl.ensure_conductor(conductor, goal="ship it")
    item = wl.apply_conductor_action(
        conductor,
        "create",
        title="build it",
        acceptance={"kind": "pr_checks", "pr": "TBD", "repo": repo},
    )["item"].item_id
    wl.apply_conductor_action(conductor, "bind", item_id=item, worker_session_key=worker)
    return item


class TestLedgerAndRollUp:
    def test_a_worker_report_puts_the_pr_on_the_conductor_card(self, tmp_path):
        # chat-537 dispatched chat-539, which reported pr=34732; chat-537's own
        # transcript never named it.
        state = _make_state(tmp_path / "sessions")
        conductor = state.get_or_create_slot("chat-537")
        conductor.append("assistant", "Dispatched the issue components work.", ts="t1")
        item = _dispatch("chat-537", "chat-539")
        wl.apply_worker_report("chat-537", item, status="progress", summary="opened", pr=34732)

        assert _urls(conductor) == [PR]
        # And on the worker's own card, from its own item.
        assert _urls(state.get_or_create_slot("chat-539")) == [PR]

    def test_an_artifacts_pr_url_is_reported_too(self, tmp_path):
        state = _make_state(tmp_path / "sessions")
        conductor = state.get_or_create_slot("chat-1")
        item = _dispatch("chat-1", "chat-2")
        wl.apply_worker_report(
            "chat-1", item, status="done", summary="done", artifacts={"pr_url": NEW}
        )
        assert _urls(conductor) == [NEW]

    def test_a_pr_the_worker_created_rolls_up_without_a_report(self, tmp_path):
        state = _make_state(tmp_path / "sessions")
        conductor = state.get_or_create_slot("chat-1")
        _dispatch("chat-1", "chat-2")
        append_created(state.get_or_create_slot("chat-2"), NEW)
        assert _urls(conductor) == [NEW]

    def test_only_one_level_and_only_what_the_worker_created(self, tmp_path):
        state = _make_state(tmp_path / "sessions")
        top = state.get_or_create_slot("chat-1")
        _dispatch("chat-1", "chat-2")
        _dispatch("chat-2", "chat-3")
        worker = state.get_or_create_slot("chat-2")
        # The worker only acted on a sweep PR: that stays on the worker's card.
        _tool(worker, "gh pr edit 22907 -R kirodotdev/KiroCrew --add-label x", _ok(""))
        # The grandchild created one: it rolls up to chat-2, not to chat-1.
        append_created(state.get_or_create_slot("chat-3"), NEW)

        assert _urls(top) == []
        assert set(_urls(worker)) == {NEW, "https://github.com/kirodotdev/KiroCrew/pull/22907"}

    def test_both_spellings_of_one_repository_are_one_chip(self, tmp_path):
        state = _make_state(tmp_path / "sessions")
        conductor = state.get_or_create_slot("chat-1")
        item = _dispatch("chat-1", "chat-2", repo="kirodotdev/kirocrew")
        wl.apply_worker_report("chat-1", item, status="progress", summary="s", pr=34742)
        append_created(conductor, NEW)  # the mixed-case spelling gh printed
        assert len(_urls(conductor)) == 1

    def test_order_follows_the_newest_owning_event_across_sources(self, tmp_path):
        state = _make_state(tmp_path / "sessions")
        conductor = state.get_or_create_slot("chat-1")
        append_created(conductor, OLD, ts="2000-01-01T00:00:00+00:00")
        item = _dispatch("chat-1", "chat-2")
        wl.apply_worker_report("chat-1", item, status="progress", summary="s", pr=34742)
        assert _urls(conductor) == [NEW, OLD]

    def test_an_unlinked_rolled_up_chip_stays_hidden(self, tmp_path):
        state = _make_state(tmp_path / "sessions")
        conductor = state.get_or_create_slot("chat-1")
        item = _dispatch("chat-1", "chat-2")
        wl.apply_worker_report("chat-1", item, status="progress", summary="s", pr=34742)
        (chip,) = conductor._pr_source_links()
        assert conductor.dismiss_source_link(chip["identity"]) is True
        assert _urls(conductor) == []

    def test_a_new_report_shows_without_a_transcript_change(self, tmp_path):
        state = _make_state(tmp_path / "sessions")
        conductor = state.get_or_create_slot("chat-1")
        item = _dispatch("chat-1", "chat-2")
        assert _urls(conductor) == []
        wl.apply_worker_report("chat-1", item, status="progress", summary="s", pr=34742)
        assert _urls(conductor) == [NEW]


class TestGhInvocations:
    def test_operators_wrappers_and_heredocs(self):
        found = ownership.gh_invocations(
            "cd /x && GH_PAGER= timeout 60 gh pr create --fill | tee o; "
            "echo gh pr merge 1\nenv -i /usr/bin/gh pr comment 2 -b hi\n"
            "cat <<'EOF' > f\ngh pr close 3\nEOF"
        )
        assert found == [["pr", "create", "--fill"], ["pr", "comment", "2", "-b", "hi"]]

    def test_meta_created_by_the_suite_helper_parses(self):
        meta = gh_create_meta(PR)
        msg = {"role": "tool", "meta": meta}
        from kiro_crew.dashboard.handlers.source_providers import (
            parse_source_url,
            source_link_path_markers,
        )
        from kiro_crew.dashboard.source_providers.links import iter_source_url_candidates

        markers = source_link_path_markers()
        owned = ownership.owned_refs_in_tool_row(
            msg, parse_source_url, lambda text: iter_source_url_candidates(text, markers)
        )
        assert [(o.ref.url, o.relation) for o in owned] == [(PR, "created")]


class TestAdversarialInputs:
    """Inputs that try to break the rule, each pinned to the answer the rule gives."""

    @pytest.mark.parametrize(
        "target",
        [pytest.param("²", id="superscript"), pytest.param("1" * 5000, id="5000-digits")],
    )
    def test_an_unreadable_number_makes_no_chip_and_raises_nothing(self, tmp_path, target):
        state = _make_state(tmp_path / "sessions")
        slot = state.get_or_create_slot("chat-1")
        _tool(slot, f"gh pr edit {target} -R o/r --add-label x", _ok(""))
        assert _urls(slot) == []
        assert state.serialize_slots()[0]["source_links_total"] == 0

    def test_a_create_after_a_heredoc_in_the_same_command_counts(self):
        slot = _ChatSlot("s1")
        _tool(
            slot,
            "cat > /tmp/b.md <<'EOF'\ngh pr close 3\nEOF\ngh pr create --body-file /tmp/b.md",
            _ok(PR),
        )
        assert _urls(slot) == [PR]

    @pytest.mark.parametrize(
        "command",
        [
            "gh pr review --comment --body LGTM 5 -R o/r",
            "gh pr review -c -b x 5 -R o/r",
            "gh pr review -a 5 -R o/r",
            "gh pr merge -r 5 -R o/r",
            "gh pr merge -m 5 -R o/r",
        ],
    )
    def test_a_verbs_own_switch_does_not_swallow_the_target(self, command):
        slot = _ChatSlot("s1")
        _tool(slot, command, _ok(""))
        assert _urls(slot) == ["https://github.com/o/r/pull/5"]

    def test_a_refused_create_behind_a_pipe_is_not_a_chip(self):
        slot = _ChatSlot("s1")
        _tool(
            slot,
            "gh pr create --fill || true",
            _ok(f'a pull request for branch "x" into branch "main" already exists:\n{PR}'),
        )
        assert _urls(slot) == []

    def test_gh_placeholders_name_no_repository(self):
        slot = _ChatSlot("s1")
        _tool(slot, "gh api repos/{owner}/{repo}/pulls/5/comments -f body=hi", _ok("{}"))
        _tool(slot, "gh pr comment 6 -R {owner}/{repo} -b hi", _ok(""))
        assert _urls(slot) == []

    def test_a_url_in_the_pr_title_cannot_redirect_a_bare_number(self):
        slot = _ChatSlot("s1")
        _tool(
            slot,
            "gh pr close 5",
            _ok("✓ Closed pull request o/r#5 (see https://github.com/evil/x/pull/5)"),
        )
        assert _urls(slot) == ["https://github.com/o/r/pull/5"]

    def test_a_non_shell_tool_needs_the_exit_trailer(self):
        slot = _ChatSlot("s1")
        slot.append(
            "tool",
            "🔧 other",
            meta={
                "input": json.dumps({"command": "gh pr create --fill"}),
                "kind": "other",
                "output": '{"ok": true}\nhttps://github.com/evil/x/pull/3',
            },
        )
        assert _urls(slot) == []

    def test_a_reported_number_without_a_repo_resolves_against_the_workers_links(self, tmp_path):
        state = _make_state(tmp_path / "sessions")
        conductor = state.get_or_create_slot("chat-1")
        _dispatch("chat-1", "chat-2", repo="")
        worker = state.get_or_create_slot("chat-2")
        _tool(worker, "gh pr comment 77 -R o/r -b hi", _ok(""))
        (item,) = wl.list_work_items("chat-1")
        wl.apply_worker_report("chat-1", item.item_id, status="progress", summary="s", pr=77)
        assert _urls(conductor) == ["https://github.com/o/r/pull/77"]

    def test_an_owner_repo_number_artifact_is_reported(self, tmp_path):
        state = _make_state(tmp_path / "sessions")
        conductor = state.get_or_create_slot("chat-1")
        item = _dispatch("chat-1", "chat-2", repo="")
        wl.apply_worker_report(
            "chat-1", item, status="progress", summary="s", artifacts={"pr": "o/r#8"}
        )
        assert _urls(conductor) == ["https://github.com/o/r/pull/8"]

    def test_a_later_progress_report_does_not_move_an_old_reported_pr_ahead(
        self, tmp_path, monkeypatch
    ):
        state = _make_state(tmp_path / "sessions")
        conductor = state.get_or_create_slot("chat-1")
        item = _dispatch("chat-1", "chat-2")
        stamps = iter(["2026-11-10T10:00:00+00:00", "2026-11-10T12:00:00+00:00"])
        monkeypatch.setattr("kiro_crew.work_ledger_writes._now_iso", lambda: next(stamps))
        wl.apply_worker_report("chat-1", item, status="progress", summary="s", pr=21991)
        assert _urls(conductor) == [OLD]
        append_created(conductor, NEW, ts="2026-11-10T11:00:00+00:00")
        wl.apply_worker_report("chat-1", item, status="progress", summary="later")
        assert _urls(conductor) == [NEW, OLD]


class TestReviewFindings:
    """Cases the local pre-push reviews raised, each pinned to the rule's answer."""

    def test_a_worker_pr_still_rolls_up_after_the_worker_edits_it(self, tmp_path):
        state = _make_state(tmp_path / "sessions")
        conductor = state.get_or_create_slot("chat-1")
        _dispatch("chat-1", "chat-2")
        worker = state.get_or_create_slot("chat-2")
        append_created(worker, "https://github.com/acme/widgets/pull/77", ts="t1")
        _tool(worker, "gh pr edit 77 -R acme/widgets --title fixed", _ok(""), ts="t2")
        assert _urls(conductor) == ["https://github.com/acme/widgets/pull/77"]

    def test_a_clear_drops_the_transcripts_chips_but_not_the_boards(self, tmp_path):
        state = _make_state(tmp_path / "sessions")
        conductor = state.get_or_create_slot("chat-1")
        item = _dispatch("chat-1", "chat-2")
        append_created(conductor, OLD, ts="2026-11-10T09:00:00+00:00")
        wl.apply_worker_report("chat-1", item, status="progress", summary="s", pr=34742)
        assert set(_urls(conductor)) == {OLD, NEW}
        conductor.append("user", "/clear", ts="2099-01-01T00:00:00+00:00")
        # The conversation started over; the work board did not.
        assert _urls(conductor) == [NEW]

    def test_an_allowlist_change_re_reads_a_memoized_row(self, monkeypatch):
        from kiro_crew.dashboard.source_providers import hosts as sp

        for name in ("_gitlab_hosts_snapshot", "_jira_hosts_snapshot", "_gitlab_hosts_loaded_at"):
            monkeypatch.setattr(sp, name, getattr(sp, name))
        monkeypatch.setattr(sp, "_github_hosts_snapshot", frozenset())
        monkeypatch.setattr(sp, "_gitlab_hosts_generation", 0)
        slot = _ChatSlot("s1")
        ghe = "https://ghe.acme.io/team/tool/pull/7"
        _tool(slot, "gh pr create --fill", _ok(ghe))
        assert _urls(slot) == []
        gitlab, jira = sp._gitlab_hosts_snapshot, sp._jira_hosts_snapshot
        sp._publish_provider_hosts(gitlab, jira, frozenset({"ghe.acme.io"}))
        assert _urls(slot) == [ghe]
        sp._publish_provider_hosts(gitlab, jira, frozenset())
        assert _urls(slot) == []

    def test_a_line_continuation_keeps_the_target(self):
        slot = _ChatSlot("s1")
        _tool(slot, "gh pr merge \\\n  42 -R acme/repo --squash", _ok(""))
        assert _urls(slot) == ["https://github.com/acme/repo/pull/42"]

    def test_another_commands_already_exists_does_not_hide_a_create(self):
        slot = _ChatSlot("s1")
        _tool(
            slot,
            "git remote add origin x || true; gh pr create --fill",
            _ok(f"error: remote origin already exists.\n{PR}"),
        )
        assert _urls(slot) == [PR]

    @pytest.mark.asyncio
    async def test_on_the_loop_the_ledger_is_read_off_it_and_the_slots_pushed(
        self, tmp_path, monkeypatch
    ):
        import asyncio
        import threading

        state = _make_state(tmp_path / "sessions")
        conductor = state.get_or_create_slot("chat-1")
        item = _dispatch("chat-1", "chat-2")
        wl.apply_worker_report("chat-1", item, status="progress", summary="s", pr=34742)
        loop_thread = threading.get_ident()
        reads: list[int] = []
        real = wl.list_work_items

        def recording(key):
            reads.append(threading.get_ident())
            return real(key)

        monkeypatch.setattr(wl, "list_work_items", recording)
        pushed = asyncio.Event()
        monkeypatch.setattr(ownership, "_ledger_refresh_hook", [pushed.set])

        assert _urls(conductor) == []  # nothing read yet, and nothing read here
        await asyncio.wait_for(pushed.wait(), 10)
        assert reads and loop_thread not in reads
        conductor.invalidate_source_links()
        assert _urls(conductor) == [NEW]


class TestReviewFindingsRoundTwo:
    def test_a_channel_conductor_reads_the_board_of_the_session_it_runs_on(self):
        slot = _ChatSlot("slack_1700000000_000100")
        slot.linked_session_key = "slack:1700000000.000100"
        item = _dispatch("slack:1700000000.000100", "chat-2")
        wl.apply_worker_report(
            "slack:1700000000.000100", item, status="progress", summary="s", pr=34742
        )
        assert _urls(slot) == [NEW]

    def test_binding_a_worker_already_read_shows_its_report_at_once(self):
        worker = _ChatSlot("chat-2")
        assert _urls(worker) == []  # read and kept before it is bound
        item = _dispatch("chat-1", "chat-2")
        wl.apply_worker_report("chat-1", item, status="progress", summary="s", pr=34742)
        assert _urls(worker) == [NEW]

    def test_an_absolute_enterprise_api_endpoint_keeps_its_host(self, monkeypatch):
        from kiro_crew.dashboard.source_providers import hosts

        monkeypatch.setattr(hosts, "_github_hosts_snapshot", frozenset({"ghe.example.com"}))
        monkeypatch.setattr(hosts, "_gitlab_hosts_generation", hosts._gitlab_hosts_generation + 1)
        slot = _ChatSlot("s1")
        _tool(
            slot,
            "gh api -X PATCH https://ghe.example.com/api/v3/repos/acme/widgets/pulls/7",
            _ok("{}"),
        )
        assert _urls(slot) == ["https://ghe.example.com/acme/widgets/pull/7"]

    def test_a_restored_sidebar_tail_keeps_the_links_its_tool_rows_owned(self):
        from kiro_crew.dashboard.chat_persistence_parts.restore_drivers import _display_tail

        slot = _ChatSlot("s1")
        _tool(slot, "gh pr create --fill", _ok(PR), ts="2026-11-10T10:00:00+00:00")
        tail = _display_tail(list(slot.messages))
        restored = _ChatSlot("s2")
        restored.messages.extend(tail)
        assert _urls(restored) == [PR]

    def test_gh_saying_owner_repo_is_not_read_as_github_com_when_enterprise_is_set(
        self, monkeypatch
    ):
        from kiro_crew.dashboard.source_providers import hosts

        monkeypatch.setattr(hosts, "_github_hosts_snapshot", frozenset({"ghe.example.com"}))
        monkeypatch.setattr(hosts, "_gitlab_hosts_generation", hosts._gitlab_hosts_generation + 1)
        slot = _ChatSlot("s1")
        _tool(slot, "gh pr merge 5 --squash", _ok("✓ Squashed and merged pull request acme/repo#5"))
        _tool(slot, "gh pr comment 6 -R acme/repo -b hi", _ok(""))
        assert _urls(slot) == []

    def test_a_file_descriptor_redirect_is_not_the_target(self):
        slot = _ChatSlot("s1")
        _tool(slot, "gh pr ready 2>&1", _ok("✓ Pull request acme/repo#9 is marked as ready"))
        assert _urls(slot) == ["https://github.com/acme/repo/pull/9"]

    def test_a_here_string_is_not_a_heredoc(self):
        slot = _ChatSlot("s1")
        _tool(slot, "grep x <<< foo\ngh pr create --fill", _ok(PR))
        assert _urls(slot) == [PR]


class TestReviewFindingsRoundThree:
    def test_a_relative_api_write_takes_the_calls_gh_host_or_makes_no_chip(self, monkeypatch):
        from kiro_crew.dashboard.source_providers import hosts

        monkeypatch.setattr(hosts, "_github_hosts_snapshot", frozenset({"ghe.example.com"}))
        monkeypatch.setattr(hosts, "_gitlab_hosts_generation", hosts._gitlab_hosts_generation + 1)
        slot = _ChatSlot("s1")
        _tool(slot, "gh api -X PATCH repos/acme/widgets/pulls/7 -f title=x", _ok("{}"))
        assert _urls(slot) == []
        _tool(
            slot,
            "GH_HOST=ghe.example.com gh api -X PATCH repos/acme/widgets/pulls/7 -f title=x",
            _ok("{}"),
        )
        _tool(slot, "GH_HOST=ghe.example.com gh pr comment 8 -R acme/widgets -b hi", _ok(""))
        assert set(_urls(slot)) == {
            "https://ghe.example.com/acme/widgets/pull/7",
            "https://ghe.example.com/acme/widgets/pull/8",
        }

    def test_before_the_host_settings_load_a_host_less_repo_makes_no_chip(self, monkeypatch):
        from kiro_crew.dashboard.chat_persistence_parts.restore_drivers import _display_tail
        from kiro_crew.dashboard.source_providers import hosts

        monkeypatch.setattr(hosts, "_gitlab_hosts_loaded_at", 0.0)
        monkeypatch.setattr(hosts, "_gitlab_hosts_generation", hosts._gitlab_hosts_generation)
        slot = _ChatSlot("s1")
        _tool(slot, "gh pr comment 8 -R acme/widgets -b hi", _ok(""))
        restored = _ChatSlot("s2")
        restored.messages.extend(_display_tail(list(slot.messages)))
        assert _urls(slot) == [] and _urls(restored) == []
        # The settings load: that first load moves the generation, so both read the chip.
        hosts._publish_provider_hosts(hosts._gitlab_hosts_snapshot, hosts._jira_hosts_snapshot)
        assert _urls(slot) == ["https://github.com/acme/widgets/pull/8"]
        assert _urls(restored) == ["https://github.com/acme/widgets/pull/8"]


class TestReviewFindingsRoundFour:
    @pytest.mark.parametrize(
        "command",
        [
            "false && gh pr comment https://github.com/acme/widgets/pull/42 --body x; true",
            "gh pr comment 42 -R acme/widgets -b x || true",
            "true || gh pr edit 42 -R acme/widgets --add-label x",
            "gh pr merge 42 -R acme/widgets --squash | tail -1",
        ],
    )
    def test_a_write_the_exit_code_does_not_vouch_for_needs_ghs_own_word(self, command):
        slot = _ChatSlot("s1")
        _tool(slot, command, _ok(""))
        assert _urls(slot) == []

    def test_a_masked_write_still_counts_when_gh_printed_the_pr(self):
        slot = _ChatSlot("s1")
        _tool(
            slot,
            "gh pr comment 42 -R acme/widgets -b x || true",
            _ok("https://github.com/acme/widgets/pull/42#issuecomment-1"),
        )
        assert _urls(slot) == ["https://github.com/acme/widgets/pull/42"]

    def test_a_redirect_and_a_following_and_keep_the_call_decisive(self):
        slot = _ChatSlot("s1")
        _tool(
            slot, "gh pr edit 42 -R acme/widgets --add-label x > /tmp/o 2>&1 && echo ok", _ok("ok")
        )
        assert _urls(slot) == ["https://github.com/acme/widgets/pull/42"]


class TestReviewFindingsRoundFive:
    @pytest.mark.parametrize(
        "command",
        [
            "gh pr comment 42 -R acme/widgets --help",
            "gh api -X PATCH repos/acme/widgets/pulls/42 --help",
        ],
    )
    def test_help_is_not_a_write(self, command):
        slot = _ChatSlot("s1")
        _tool(slot, command, _ok("Usage: gh ..."))
        assert _urls(slot) == []

    def test_the_restored_tail_redacts_the_gh_payload(self):
        from kiro_crew.dashboard.chat_persistence_parts.restore_drivers import _display_tail

        secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        slot = _ChatSlot("s1")
        _tool(slot, f"GH_TOKEN={secret} gh pr create --fill", _ok(f"{PR}\ntoken {secret}"))
        (row,) = _display_tail(list(slot.messages))
        assert secret not in row["meta"]["input"] and secret not in row["meta"]["output"]

    def test_a_long_lived_board_keeps_only_the_newest_reports(self, monkeypatch):
        item_ids = [_dispatch("chat-1", f"chat-{n}") for n in range(2, 4)]
        for n, item in enumerate(item_ids, start=1):
            wl.apply_worker_report("chat-1", item, status="progress", summary="s", pr=n)
        monkeypatch.setattr(ownership, "MAX_LEDGER_LINKS", 1)
        answer = ownership.ledger_links("chat-1", *_parsers())
        assert len(answer.links) == 1 and len(answer.workers) == 1


def _parsers():
    from kiro_crew.dashboard.handlers.source_providers import (
        parse_source_url,
        source_link_path_markers,
    )
    from kiro_crew.dashboard.source_providers.links import iter_source_url_candidates

    markers = source_link_path_markers()
    return parse_source_url, lambda text: iter_source_url_candidates(text, markers)


class TestReviewFindingsRoundFiveOpus:
    def test_a_direct_argv_create_counts(self):
        slot = _ChatSlot("s1")
        slot.append(
            "tool",
            "🔧 run",
            meta={
                "input": json.dumps({"command": ["gh", "pr", "create", "--fill"]}),
                "kind": "execute",
                "output": _ok(PR),
            },
        )
        assert _urls(slot) == [PR]

    @pytest.mark.asyncio
    async def test_an_evicted_answer_that_did_not_change_pushes_nothing(self, monkeypatch):
        import asyncio

        item = _dispatch("chat-1", "chat-2")
        wl.apply_worker_report("chat-1", item, status="progress", summary="s", pr=34742)
        pushes: list[int] = []
        landed = asyncio.Event()
        monkeypatch.setattr(
            ownership, "_ledger_refresh_hook", [lambda: (pushes.append(1), landed.set())]
        )
        ownership.ledger_links("chat-1", *_parsers())
        await asyncio.wait_for(landed.wait(), 10)
        assert pushes == [1]
        # The cache lets the answer go; the next read lands the same answer.
        ownership._ledger_cache.clear()
        ownership.ledger_links("chat-1", *_parsers())
        while ownership._ledger_inflight:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert pushes == [1]


class TestReviewFindingsRoundSix:
    def test_a_url_another_gh_call_names_is_not_the_creates(self):
        slot = _ChatSlot("s1")
        _tool(
            slot,
            "gh pr create --fill && gh pr view 2 -R acme/widgets --json url --jq .url",
            _ok(f"{PR}\nhttps://github.com/acme/widgets/pull/2"),
        )
        assert _urls(slot) == [PR]

    def test_an_absolute_path_argv_create_counts(self):
        slot = _ChatSlot("s1")
        slot.append(
            "tool",
            "🔧 run",
            meta={
                "input": json.dumps({"command": ["/usr/bin/gh", "pr", "create", "--fill"]}),
                "kind": "execute",
                "output": _ok(PR),
            },
        )
        assert _urls(slot) == [PR]

    def test_many_gh_calls_over_a_large_output_stay_cheap(self):
        import time

        calls = " ; ".join(f"gh pr comment {n} -b x" for n in range(100))
        command = calls + "\n" * 2000
        output = "x" * 1_000_000
        msg = {
            "role": "tool",
            "meta": {
                "input": json.dumps({"command": command}),
                "kind": "execute",
                "output": _ok(output),
            },
        }
        started = time.perf_counter()
        ownership.owned_refs_in_tool_row(msg, *_parsers())
        assert time.perf_counter() - started < 1.0
