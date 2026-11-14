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

from kiro_crew import agent_sdk
from kiro_crew import work_ledger as wl
from kiro_crew.dashboard.source_providers import ownership
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.testing.wait import async_wait_until, await_until

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


def _row_refs(command: str, output: str) -> list:
    """The owned refs one successful shell row with *command* and *output* yields."""
    meta = {"input": json.dumps({"command": command}), "kind": "execute", "output": _ok(output)}
    return ownership.owned_refs_in_tool_row({"role": "tool", "meta": meta}, *_parsers())


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

    def test_an_unlink_on_the_workers_card_leaves_the_conductors_chip(self, tmp_path):
        state = _make_state(tmp_path / "sessions")
        conductor = state.get_or_create_slot("chat-1")
        _dispatch("chat-1", "chat-2")
        worker = state.get_or_create_slot("chat-2")
        append_created(worker, NEW)
        assert _urls(conductor) == [NEW]
        (chip,) = worker._pr_source_links()
        assert worker.dismiss_source_link(chip["identity"]) is True
        assert _urls(worker) == []
        assert _urls(conductor) == [NEW]

    def test_an_unlink_on_the_conductors_card_hides_a_rolled_up_create(self, tmp_path):
        state = _make_state(tmp_path / "sessions")
        conductor = state.get_or_create_slot("chat-1")
        _dispatch("chat-1", "chat-2")
        worker = state.get_or_create_slot("chat-2")
        append_created(worker, NEW)
        (chip,) = conductor._pr_source_links()
        assert conductor.dismiss_source_link(chip["identity"]) is True
        assert _urls(conductor) == []
        assert _urls(worker) == [NEW]

    def test_a_hidden_link_does_not_take_a_visible_ones_place(self):
        from kiro_crew.dashboard.slot_projection import SlotProjection

        slot = _ChatSlot("s1")
        for number in (1, 2, 3):
            append_created(slot, f"https://github.com/acme/widgets/pull/{number}")
        newest = slot._pr_source_links()[0]
        assert slot.dismiss_source_link(newest["identity"]) is True
        links = SlotProjection.source_links(slot, max_links=2, non_durable_roles=frozenset())
        assert [link["number"] for link in links] == [2, 1]
        every = SlotProjection.source_links(
            slot, max_links=2, non_durable_roles=frozenset(), keep_dismissed=True
        )
        assert [link["number"] for link in every] == [3, 2, 1]

    def test_a_new_report_shows_without_a_transcript_change(self, tmp_path):
        state = _make_state(tmp_path / "sessions")
        conductor = state.get_or_create_slot("chat-1")
        item = _dispatch("chat-1", "chat-2")
        assert _urls(conductor) == []
        wl.apply_worker_report("chat-1", item, status="progress", summary="s", pr=34742)
        assert _urls(conductor) == [NEW]


class TestGhInvocations:
    def test_operators_wrappers_and_heredocs(self):
        found = [
            call.argv
            for call in ownership._gh_calls(
                "cd /x && GH_PAGER= timeout 60 gh pr create --fill | tee o; "
                "echo gh pr merge 1\nenv -i /usr/bin/gh pr comment 2 -b hi\n"
                "cat <<'EOF' > f\ngh pr close 3\nEOF"
            )
        ]
        assert found == [["pr", "create", "--fill"], ["pr", "comment", "2", "-b", "hi"]]

    def test_quoting_redirections_and_line_continuations(self):
        found = [
            call.argv
            for call in ownership._gh_calls(
                'gh pr comment 5 -R o/r -b "it\'s \\"ok\\"" 2>&1 \\\n  --edit-last'
            )
        ]
        assert found == [["pr", "comment", "5", "-R", "o/r", "-b", 'it\'s "ok"', "--edit-last"]]

    def test_a_large_command_with_no_gh_write_never_reaches_the_shell_reader(self):
        command = "echo " + "word " * 14_000 + "&& gh pr view 3"
        assert len(command) > 64 * 1024
        msg = {
            "role": "tool",
            "meta": {"input": json.dumps({"command": command}), "kind": "execute"},
        }
        msg["meta"]["output"] = _ok("")
        before = ownership._gh_calls.cache_info()
        assert ownership.owned_refs_in_tool_row(msg, *_parsers()) == []
        assert ownership._gh_calls.cache_info() == before

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
        now = ["2026-11-10T10:00:00+00:00"]
        monkeypatch.setattr(wl, "_now_iso", lambda: now[0])
        wl.apply_worker_report("chat-1", item, status="progress", summary="s", pr=21991)
        assert _urls(conductor) == [OLD]
        append_created(conductor, NEW, ts="2026-11-10T11:00:00+00:00")
        now[0] = "2026-11-10T12:00:00+00:00"
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

        # Recorder first: creating the slot can already queue a ledger read.
        loop_thread = threading.get_ident()
        reads: list[int] = []
        real = wl.list_work_items

        def recording(key):
            reads.append(threading.get_ident())
            return real(key)

        monkeypatch.setattr(wl, "list_work_items", recording)
        state = _make_state(tmp_path / "sessions")
        conductor = state.get_or_create_slot("chat-1")
        item = _dispatch("chat-1", "chat-2")
        wl.apply_worker_report("chat-1", item, status="progress", summary="s", pr=34742)
        ownership.reset_ledger_cache()
        reads.clear()
        pushed = asyncio.Event()
        monkeypatch.setattr(ownership, "_ledger_refresh_hook", [pushed.set])

        assert _urls(conductor) == []  # nothing read yet, and nothing read here
        # A read still landing from an earlier test can fire the hook too, so wait
        # for this test's own off-loop read, then for the push it brings.
        await async_wait_until(lambda: bool(reads))
        assert loop_thread not in reads
        await await_until(pushed)
        await async_wait_until(
            lambda: (conductor.invalidate_source_links(), _urls(conductor))[1] == [NEW]
        )


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
        await await_until(landed)
        assert pushes == [1]
        # The cache lets the answer go; the next read lands the same answer.
        ownership._ledger_cache.clear()
        ownership.ledger_links("chat-1", *_parsers())
        # ``landed`` clears the in-flight key and runs the push hooks in one loop
        # callback, so once the key is gone any push has already happened.
        await async_wait_until(lambda: not ownership._ledger_inflight)
        assert pushes == [1]


class TestReviewFindingsRoundSix:
    """Another gh call's printed URL is never the create's, nor proof of a write."""

    @pytest.mark.parametrize("status", ["refused", "failed", "cancelled", "canceled"])
    @pytest.mark.parametrize(
        ("command", "output"),
        [
            pytest.param("gh pr merge https://github.com/o/r/pull/5 --squash", "", id="url-target"),
            pytest.param(
                "gh pr comment 5 -R o/r -b x", "Command was denied by the user.", id="denied"
            ),
            pytest.param("gh pr create --fill", "https://github.com/o/r/pull/9", id="create"),
        ],
    )
    def test_a_write_that_did_not_complete_makes_no_chip(self, command, output, status):
        slot = _ChatSlot("s1")
        meta = {
            "input": json.dumps({"command": command}),
            "kind": "execute",
            "output": output,
            "tool_status": status,
            "done": True,
        }
        slot.append("tool", "🔧 Run a command", meta=meta)
        assert _urls(slot) == []

    def test_a_write_with_no_trailer_and_no_status_makes_no_chip(self):
        # Still running, or from a backend that says neither: nothing proves it ran.
        slot = _ChatSlot("s1")
        meta = {
            "input": json.dumps({"command": "gh pr comment 5 -R o/r -b x"}),
            "kind": "execute",
            "output": "",
        }
        slot.append("tool", "🔧 Run a command", meta=meta)
        assert _urls(slot) == []

    def test_a_completed_status_with_no_trailer_counts(self):
        slot = _ChatSlot("s1")
        meta = {
            "input": json.dumps({"command": "gh pr comment 5 -R o/r -b x"}),
            "kind": "execute",
            "output": "https://github.com/o/r/pull/5#issuecomment-1",
            "tool_status": "completed",
        }
        slot.append("tool", "🔧 Run a command", meta=meta)
        assert _urls(slot) == ["https://github.com/o/r/pull/5"]

    def test_a_status_landing_after_the_output_drops_the_chip(self):
        slot = _ChatSlot("s1")
        meta = {
            "input": json.dumps({"command": "gh pr merge 5 -R o/r --squash"}),
            "kind": "execute",
            "output": "",
            "tool_status": "completed",
        }
        slot.append("tool", "🔧 Run a command", meta=meta)
        assert _urls(slot) == ["https://github.com/o/r/pull/5"]
        slot.messages[-1]["meta"]["tool_status"] = "failed"
        slot.invalidate_source_links()
        assert _urls(slot) == []

    @pytest.mark.parametrize(
        "read",
        [
            pytest.param("gh pr view 2 --json url --jq .url", id="view"),
            pytest.param("gh pr view '#2' --json url -q .url", id="view-hash"),
            pytest.param("gh pr checks 2 --json link", id="checks"),
            pytest.param("gh pr comment 2 -b hi", id="comment"),
            pytest.param("gh api repos/{owner}/{repo}/pulls/2 --jq .html_url", id="api"),
            pytest.param("gh pr merge 2 --squash", id="merge"),
            pytest.param("gh pr close 2", id="close"),
            pytest.param("gh pr review 2 --approve", id="review"),
            pytest.param("gh pr edit 2 --add-label x", id="edit"),
            pytest.param("gh pr diff 2 --name-only", id="diff"),
            pytest.param("gh api repos/acme/w/issues/2/comments -f body=x", id="api-issues"),
        ],
    )
    def test_a_bare_numbered_call_after_a_create_is_not_created(self, read):
        other = "https://github.com/kirodotdev/KiroCrew/pull/2"
        owned = _row_refs(f"gh pr create --fill && {read}", f"{PR}\n{other}")
        assert [o.ref.url for o in owned if o.relation == "created"] == [PR]

    def test_a_bare_issue_view_after_an_issue_create_is_not_created(self):
        new = "https://github.com/acme/widgets/issues/9"
        other = "https://github.com/acme/widgets/issues/2"
        owned = _row_refs(
            "gh issue create --fill && gh issue view 2 --json url -q .url", f"{new}\n{other}"
        )
        assert [o.ref.url for o in owned if o.relation == "created"] == [new]

    def test_a_masked_write_is_not_confirmed_by_another_calls_read(self):
        slot = _ChatSlot("s1")
        _tool(
            slot,
            "gh pr comment 5 -R other/lib -b done || true; gh pr view 5 -R other/lib --json url -q .url",
            _ok("https://github.com/other/lib/pull/5"),
        )
        assert _urls(slot) == []

    def test_a_bare_write_does_not_take_another_repos_read(self):
        slot = _ChatSlot("s1")
        _tool(
            slot,
            "gh pr view 5 -R other/lib --json url -q .url && gh pr edit 5 --add-label x",
            _ok("https://github.com/other/lib/pull/5"),
        )
        assert _urls(slot) == []

    def test_a_bare_write_still_takes_its_own_printed_pr(self):
        slot = _ChatSlot("s1")
        _tool(
            slot,
            "gh pr edit 5 --add-label x && gh pr view 5 --json title",
            _ok("https://github.com/acme/widgets/pull/5\n{}"),
        )
        assert _urls(slot) == ["https://github.com/acme/widgets/pull/5"]

    @pytest.mark.parametrize(
        ("command", "gh_url"),
        [
            pytest.param(
                "cat /tmp/pr-body.md && gh pr edit --body-file /tmp/pr-body.md", PR, id="no-target"
            ),
            pytest.param("git log -1 --format=%B && gh pr comment -b done", PR, id="comment"),
            pytest.param(
                "echo notes; gh pr edit feat -R acme/lib --add-label x",
                "https://github.com/acme/lib/pull/3",
                id="branch",
            ),
            pytest.param(
                "cat notes.txt; gh pr edit 88 --add-label z",
                "https://github.com/acme/widgets/pull/88",
                id="bare-number",
            ),
        ],
    )
    def test_another_programs_url_is_not_a_writes_evidence(self, command, gh_url):
        # Real gh prints its own URL after the other program's; two candidates
        # mean the row cannot say which PR gh wrote to.
        slot = _ChatSlot("s1")
        _tool(slot, command, _ok(f"Follow-up to\nhttps://github.com/acme/lib/pull/88\n{gh_url}"))
        assert _urls(slot) == []

    def test_a_masked_write_takes_no_evidence_from_another_programs_output(self):
        slot = _ChatSlot("s1")
        _tool(
            slot,
            "cat notes.txt; gh pr comment 88 -R acme/lib -b x || true",
            _ok("Follow-up to\nhttps://github.com/acme/lib/pull/88"),
        )
        assert _urls(slot) == []

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push && gh pr comment 42 --body hi", id="push-then-comment"),
            pytest.param("cat <<'EOF' | gh pr comment 42 --body-file -\nhi\nEOF", id="piped-body"),
            pytest.param("echo body | gh pr comment 42 -F -", id="piped-echo"),
        ],
    )
    def test_an_unambiguous_write_after_another_program_keeps_its_chip(self, command):
        slot = _ChatSlot("s1")
        _tool(
            slot,
            command,
            _ok(
                "To github.com:acme/widgets.git\n   a..b  feat -> feat\n"
                "https://github.com/acme/widgets/pull/42#issuecomment-111"
            ),
        )
        assert _urls(slot) == ["https://github.com/acme/widgets/pull/42"]

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(
                "if ! gh pr view --json url >/dev/null 2>&1; then gh pr create --fill; fi",
                id="if-then",
            ),
            pytest.param("{ gh pr create --fill; }", id="brace-group"),
        ],
    )
    def test_a_create_inside_a_shell_block_is_found(self, command):
        slot = _ChatSlot("s1")
        _tool(slot, command, _ok("https://github.com/acme/widgets/pull/50"))
        assert _urls(slot) == ["https://github.com/acme/widgets/pull/50"]

    def test_a_write_inside_a_loop_is_found(self):
        slot = _ChatSlot("s1")
        _tool(
            slot,
            "for n in 1; do gh pr edit 42 -R acme/widgets --add-label x; done",
            _ok("https://github.com/acme/widgets/pull/42"),
        )
        assert _urls(slot) == ["https://github.com/acme/widgets/pull/42"]

    def test_a_guarded_write_behind_a_quoted_substitution_is_not_decisive(self):
        # ``[ -f x ] || gh ...``: the guard can be true, so gh may never run.
        slot = _ChatSlot("s1")
        _tool(
            slot, '[ -f /tmp/posted ] || gh pr comment 42 -R o/r --body "$(cat /tmp/s.md)"', _ok("")
        )
        assert _urls(slot) == []

    def test_a_substitution_body_does_not_break_an_and_chain(self):
        slot = _ChatSlot("s1")
        command = (
            "gh pr merge 41 -R o/r --squash && gh pr comment 42 -R o/r --body \"$(cat <<'EOF'\n"
            'hi\nEOF\n)"'
        )
        _tool(slot, command, _ok("https://github.com/o/r/pull/42#issuecomment-1"))
        assert set(_urls(slot)) == {
            "https://github.com/o/r/pull/41",
            "https://github.com/o/r/pull/42",
        }

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("gh pr comment 42 -R o/r -b x 2>&1 | tail -3", id="tail"),
            pytest.param("gh pr edit 42 -R o/r --add-label x | cat", id="cat"),
            pytest.param(
                "if [ -f /tmp/b.md ]; then gh pr comment 42 -R o/r -F /tmp/b.md; fi", id="test"
            ),
        ],
    )
    def test_a_masked_write_whose_only_other_command_adds_no_url_keeps_its_chip(self, command):
        slot = _ChatSlot("s1")
        _tool(slot, command, _ok("https://github.com/o/r/pull/42#issuecomment-1"))
        assert _urls(slot) == ["https://github.com/o/r/pull/42"]

    def test_a_create_in_a_quoted_substitution_is_found(self):
        slot = _ChatSlot("s1")
        _tool(slot, 'url="$(gh pr create --fill)" && echo "$url"', _ok(PR))
        assert _urls(slot) == [PR]

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(
                'BODY=$(cat /tmp/b.md) && gh pr comment 42 -R o/r --body "$BODY"', id="dollar"
            ),
            pytest.param("BODY=`cat b.md` && gh pr comment 42 -R o/r --body x", id="backtick"),
            pytest.param(
                "cd $(git rev-parse --show-toplevel) && gh pr comment 42 -R o/r -b x", id="cd"
            ),
            pytest.param("gh pr comment 42 --body $(cat b.md) -R o/r", id="inside-args"),
        ],
    )
    def test_an_unquoted_substitution_keeps_a_writes_chip(self, command):
        slot = _ChatSlot("s1")
        _tool(slot, command, _ok("https://github.com/o/r/pull/42#issuecomment-1"))
        assert _urls(slot) == ["https://github.com/o/r/pull/42"]

    def test_a_create_in_an_unquoted_substitution_is_found(self):
        slot = _ChatSlot("s1")
        _tool(slot, "url=$(gh pr create --fill) && echo $url", _ok(PR))
        assert _urls(slot) == [PR]

    def test_a_subshell_write_is_not_decisive(self):
        slot = _ChatSlot("s1")
        _tool(slot, "(cd x && gh pr edit 1 -R o/r --add-label y); true", _ok(""))
        assert _urls(slot) == []

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(
                "gh pr comment 42 -R o/r --body-file - <<EOF\n"
                "It's ready at $(git rev-parse --short HEAD).\nEOF",
                id="apostrophe-dollar",
            ),
            pytest.param(
                "gh pr comment 42 -R o/r --body-file - <<EOF\nThis doesn't change `foo`.\nEOF",
                id="apostrophe-backtick",
            ),
            pytest.param(
                "cat > /tmp/b.md <<'EOF'\nThis doesn't change the API.\nEOF\n"
                "gh pr comment 42 -R o/r --body-file /tmp/b.md && echo done at $(date)",
                id="quoted-heredoc-then-subst",
            ),
        ],
    )
    def test_heredoc_prose_does_not_hide_a_writes_chip(self, command):
        slot = _ChatSlot("s1")
        _tool(slot, command, _ok("https://github.com/o/r/pull/42#issuecomment-1"))
        assert "https://github.com/o/r/pull/42" in _urls(slot)

    def test_a_write_inside_a_heredoc_substitution_is_not_decisive(self):
        slot = _ChatSlot("s1")
        _tool(slot, "cat <<EOF\nIt's $(gh pr close 5 -R o/r)\nEOF", _ok(""))
        assert _urls(slot) == []

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(
                'PR=$(gh pr view --json number -q .number) && gh pr edit "$PR" --add-label bug',
                id="variable",
            ),
            pytest.param(
                'gh pr edit "$(gh pr list --head b --json number -q .[0].number)" --add-label bug',
                id="inline",
            ),
        ],
    )
    def test_a_substituted_read_does_not_stop_a_write_being_alone(self, command):
        slot = _ChatSlot("s1")
        _tool(slot, command, _ok("https://github.com/o/r/pull/42"))
        assert _urls(slot) == ["https://github.com/o/r/pull/42"]

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(
                "gh pr merge 5 -R o/r --squash --match-head-commit $(git rev-parse HEAD) 2>&1 | tail -5",
                id="dollar",
            ),
            pytest.param(
                "gh pr merge 5 -R o/r --squash --match-head-commit `git rev-parse HEAD` | tail -5",
                id="backtick",
            ),
        ],
    )
    def test_a_masked_write_with_a_substituted_arg_keeps_its_chip(self, command):
        slot = _ChatSlot("s1")
        _tool(slot, command, _ok("✓ Squashed and merged pull request o/r#5 (fix)"))
        assert _urls(slot) == ["https://github.com/o/r/pull/5"]

    def test_a_negated_write_is_not_decisive(self):
        slot = _ChatSlot("s1")
        _tool(slot, "! gh pr merge 5 -R o/r --squash", _ok(""))
        assert _urls(slot) == []

    def test_a_list_limit_is_not_a_bare_number(self):
        owned = _row_refs(
            "gh pr create --fill && gh pr list --limit 20 --json url -q '.[].url'",
            "https://github.com/acme/widgets/pull/20",
        )
        assert [o.ref.number for o in owned if o.relation == "created"] == [20]

    def test_a_guard_ending_its_line_with_or_is_not_decisive(self):
        slot = _ChatSlot("s1")
        _tool(slot, "[ -f /tmp/posted ] ||\n  gh pr comment 42 -R o/r -F /tmp/s.md", _ok(""))
        assert _urls(slot) == []

    @pytest.mark.parametrize(
        ("command", "output"),
        [
            pytest.param(
                "gh pr merge 42 -R o/r --squash --delete-branch &&\n  git checkout main &&\n"
                "  git pull --ff-only",
                "",
                id="merge-chain",
            ),
            pytest.param(
                "gh pr comment 42 -R o/r -b x &&\n  git push",
                "https://github.com/o/r/pull/42#issuecomment-1",
                id="comment-then-push",
            ),
            pytest.param(
                "gh pr comment 42 -R o/r -b x 2>&1 |\n  tail -3",
                "https://github.com/o/r/pull/42#issuecomment-1",
                id="pipe-on-next-line",
            ),
        ],
    )
    def test_an_operator_ending_its_line_keeps_the_chain(self, command, output):
        slot = _ChatSlot("s1")
        _tool(slot, command, _ok(output))
        assert _urls(slot) == ["https://github.com/o/r/pull/42"]

    def test_a_silent_cd_does_not_hide_a_writes_own_output(self):
        slot = _ChatSlot("s1")
        _tool(slot, "cd ~/repo && gh pr edit --add-label x", _ok(PR))
        assert _urls(slot) == [PR]

    def test_a_report_keeps_its_pr_when_an_artifact_in_another_repo_shares_the_number(
        self, tmp_path
    ):
        state = _make_state(tmp_path / "sessions")
        conductor = state.get_or_create_slot("chat-1")
        item = _dispatch("chat-1", "chat-2", repo="acme/widgets")
        other = "https://github.com/other/lib/pull/42"
        wl.apply_worker_report(
            "chat-1", item, status="progress", summary="s", pr=42, artifacts={"dep": other}
        )
        assert set(_urls(conductor)) == {"https://github.com/acme/widgets/pull/42", other}

    @pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
    def test_a_reading_api_method_is_not_acted_on(self, method):
        slot = _ChatSlot("s1")
        _tool(slot, f"gh api -X {method} repos/acme/widgets/pulls/42", _ok(""))
        assert _urls(slot) == []

    def test_an_attached_short_field_makes_the_api_call_a_write(self):
        slot = _ChatSlot("s1")
        _tool(slot, "gh api repos/acme/widgets/pulls/42/reviews -fevent=APPROVE --silent", _ok(""))
        assert _urls(slot) == ["https://github.com/acme/widgets/pull/42"]

    def test_a_branch_targeted_write_takes_the_pr_gh_printed(self):
        slot = _ChatSlot("s1")
        url = "https://github.com/acme/widgets/pull/7#issuecomment-1"
        _tool(slot, "gh pr comment feature/fix -R acme/widgets -b done", _ok(url))
        assert _urls(slot) == ["https://github.com/acme/widgets/pull/7"]

    def test_a_branch_targeted_write_ignores_a_pr_in_another_repo(self):
        slot = _ChatSlot("s1")
        url = "https://github.com/other/lib/pull/7#issuecomment-1"
        _tool(slot, "gh pr comment feature/fix -R acme/widgets -b done", _ok(url))
        assert _urls(slot) == []

    def test_a_branch_targeted_write_among_other_calls_makes_no_chip(self):
        slot = _ChatSlot("s1")
        url = "https://github.com/acme/widgets/pull/7#issuecomment-1"
        _tool(
            slot,
            "gh pr comment feature/fix -R acme/widgets -b done && gh pr view 3 -R acme/w",
            _ok(url),
        )
        assert _urls(slot) == []

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

    def test_many_gh_calls_over_a_large_output_read_its_tail_once(self):
        # Every call is masked (``;``), so each asks what gh printed: the row
        # still reads its bounded output tail once, not once per call.
        calls = " ; ".join(f"gh pr comment {n} -R acme/widgets -b x" for n in range(100))
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
        parse, candidates = _parsers()
        scanned: list[int] = []

        def counting(text: str):
            scanned.append(len(text))
            return candidates(text)

        owned = ownership.owned_refs_in_tool_row(msg, parse, counting)
        # Only the last call is not masked by a ``;`` after it.
        assert [(r.ref.number, r.relation) for r in owned] == [(99, "acted")]
        assert len(ownership._gh_calls(command)) == 100
        assert scanned == [ownership.OUTPUT_SCAN_CHARS]


class TestReviewFindingsRoundEight:
    def test_a_reopen_is_a_write_as_the_batch_guard_reads_it(self):
        slot = _ChatSlot("s1")
        _tool(slot, "gh pr reopen 5 -R o/r", _ok("✓ Reopened pull request o/r#5"))
        assert _urls(slot) == ["https://github.com/o/r/pull/5"]

    def test_a_backgrounded_write_is_not_proven(self):
        slot = _ChatSlot("s1")
        _tool(slot, "gh pr merge 42 -R acme/widgets --squash &", _ok(""))
        assert _urls(slot) == []

    def test_a_create_behind_a_read_that_printed_is_not_credited(self):
        owned = _row_refs("gh pr view --json url --jq .url || gh pr create --fill", PR)
        assert [o.relation for o in owned] == []

    def test_a_masked_create_alone_still_counts(self):
        owned = _row_refs("gh pr create --fill || true", PR)
        assert [(o.ref.url, o.relation) for o in owned] == [(PR, "created")]

    def test_a_restored_tail_keeps_only_a_short_status(self):
        from kiro_crew.dashboard.chat_persistence_parts.restore_drivers import _tail_gh_meta

        meta = gh_create_meta(PR)
        assert _tail_gh_meta({**meta, "tool_status": "completed"})["tool_status"] == "completed"
        assert "tool_status" not in _tail_gh_meta({**meta, "tool_status": "x" * 5000})

    def test_a_worker_rebound_to_another_board_no_longer_rolls_up(self, tmp_path):
        state = _make_state(tmp_path / "sessions")
        first = state.get_or_create_slot("chat-1")
        item = _dispatch("chat-1", "chat-2")
        worker = state.get_or_create_slot("chat-2")
        append_created(worker, NEW)
        assert _urls(first) == [NEW]
        wl.apply_conductor_action("chat-1", "close", item_id=item, state="abandoned")
        _dispatch("chat-3", "chat-2")
        ownership.reset_ledger_cache()
        first.invalidate_source_links()
        assert _urls(first) == []
        assert _urls(state.get_or_create_slot("chat-3")) == [NEW]


class TestShellWhitespace:
    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("gh  pr create --fill", id="two-spaces"),
            pytest.param("gh\tpr create --fill", id="tab"),
            pytest.param("gh pr\tcreate --fill", id="tab-after-pr"),
            pytest.param("cd x &&\tgh  pr  create --fill", id="mixed"),
        ],
    )
    def test_a_create_spelled_with_other_whitespace_is_a_chip(self, command):
        slot = _ChatSlot("s1")
        _tool(slot, command, _ok(PR))
        assert _urls(slot) == [PR]


class TestTerminalStatusReachesTheRow:
    """The live tool-result path stores the call's terminal status on its row."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "output", "expected"),
        [
            pytest.param("failed", "", [], id="failed"),
            pytest.param("refused", "Command was denied by the user.", [], id="refused"),
            pytest.param("cancelled", "", [], id="cancelled"),
            pytest.param(
                "completed",
                "Exit Code: 0",
                ["https://github.com/o/r/pull/5"],
                id="completed",
            ),
        ],
    )
    async def test_a_url_targeted_merge_counts_only_when_it_completed(
        self, tmp_path, monkeypatch, status, output, expected
    ):
        from test_nothing_to_do_directive import _drive_turn, _stub_state

        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            AcpEvent,
        )

        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot(f"gh-merge-{status}")
        slot._titled = True
        command = "gh pr merge https://github.com/o/r/pull/5 --squash"
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc-merge",
                title=f"Running: {command}",
                is_shell=True,
                tool_input=json.dumps({"command": command}),
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="tc-merge",
                tool_output=output,
                tool_final=status == "completed",
                tool_status=status,
            ),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        await _drive_turn(state, slot, events, monkeypatch, self_wake=False)
        statuses = [m["meta"].get("tool_status") for m in slot.messages if m.get("role") == "tool"]
        assert statuses and all(s == status for s in statuses), statuses
        assert _urls(slot) == expected


class TestApiEndpointMatchesTheGuard:
    """The card reads a ``gh api`` endpoint with the batch guard's own valued-option
    table, so for every option in it both skip its value and find the same endpoint."""

    @pytest.mark.parametrize("flag", sorted(agent_sdk.gh_valued_options()))
    def test_the_card_and_the_guard_find_the_same_endpoint(self, flag):
        # A value shaped like another endpoint, so reading it as the endpoint shows.
        value = "github.com" if flag == "--hostname" else "repos/x/y/pulls/9"
        endpoint = "repos/kirodotdev/KiroCrew/pulls/7"
        call = ["api", flag, value, endpoint]
        guard = agent_sdk.command_positionals(["gh", *call], agent_sdk.gh_valued_options())
        assert guard[:2] == ["api", endpoint]
        parse, _candidates = _parsers()
        url = "https://github.com/kirodotdev/KiroCrew/pull/7"
        assert ownership._named_target(call, "github.com", parse).url == url
        assert ownership._api_ref(call[1:], parse, "github.com").url == url
        bare = ["api", flag, value, "repos/{owner}/{repo}/pulls/7"]
        assert ownership._bare_number(bare, "github.com", parse) == 7
