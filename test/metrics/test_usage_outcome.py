"""What a session delivered, read from its own transcript and set beside its spend.

``usage_outcome`` counts only what a tool row proves: a commit needs git's own
summary line in the row's output, a pull request needs its URL on a line of its
own, and "merged" comes from the gateway's cached chip status, never from a
provider call. Every rule fails toward counting less, so the tests here pin the
shapes that must NOT count as much as the ones that must.
"""

import asyncio
import json
import math
import threading
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.chat_utils import slot_transcript_key
from kiro_crew.dashboard.handlers import telemetry as h
from kiro_crew.dashboard.handlers import usage_outcome as uo
from kiro_crew.dashboard.handlers.usage_store import TimeWindow
from kiro_crew.dashboard.source_providers import chip_status
from kiro_crew.history import ConversationLog
from kiro_crew.history_derivation import TranscriptWithheld
from kiro_crew.history_files import transcript_lock_stems

PR_URL = "https://github.com/acme/widgets/pull/41"
OTHER_URL = "https://github.com/acme/widgets/pull/42"
T0 = "2026-11-10T12:00:00+00:00"
T1 = "2026-11-11T12:00:00+00:00"
EPOCH0 = 1794312000.0  # 2026-11-10T12:00:00Z


@pytest.fixture(autouse=True)
def _fresh_memo():
    uo._memo.clear()
    yield
    uo._memo.clear()


def _tool(
    command: str,
    output,
    ts: str = T0,
    *,
    done: bool = True,
    call_id: str = "c1",
    full_command: str | None = None,
) -> dict:
    meta: dict = {"tool_call_id": call_id, "done": done}
    if output is not None:
        meta["output"] = output
    if full_command is not None:
        meta["input"] = json.dumps({"command": full_command})
    return {"role": "tool", "content": f"\U0001f527 {command}", "ts": ts, "meta": meta}


COMMIT_OUT = "[feat/x 1a2b3c4] add the thing\n 2 files changed, 10 insertions(+), 3 deletions(-)\n"


def _kinds(events) -> list[str]:
    return [e[0] for e in events]


class TestCommits:
    def test_a_commit_is_counted_with_its_changed_lines(self):
        events = uo.outcome_events([_tool('git commit -m "add the thing"', COMMIT_OUT)])
        assert events == (("commit", EPOCH0, 10, 3, "1a2b3c4"),)

    def test_a_commit_with_only_insertions_has_zero_removed(self):
        out = "[main 9f8e7d6] seed\n 1 file changed, 4 insertions(+)\n"
        (event,) = uo.outcome_events([_tool("git add . && git commit -m seed", out)])
        assert event[2:4] == (4, 0)

    def test_a_root_commit_header_counts(self):
        out = "[main (root-commit) abc1234] first\n 1 file changed, 1 insertion(+)\n"
        (event,) = uo.outcome_events([_tool("git commit -m first", out)])
        assert event[4] == "abc1234" and event[2] == 1

    def test_two_commits_in_one_command_each_take_their_own_lines(self):
        out = (
            "[main aaaaaaa] one\n 1 file changed, 2 insertions(+)\n"
            "[main bbbbbbb] two\n 1 file changed, 5 deletions(-)\n"
        )
        events = uo.outcome_events([_tool("git commit -m one; git commit -m two", out)])
        assert [(e[2], e[3]) for e in events] == [(2, 0), (0, 5)]

    def test_the_lines_come_from_the_statistics_line_never_the_subject(self):
        out = "[main 1a2b3c4] fix parsing of 999 insertions(+)\n 1 file changed, 2 insertions(+)\n"
        (event,) = uo.outcome_events([_tool('git commit -m "fix parsing"', out)])
        assert event[2:4] == (2, 0)

    def test_a_later_commands_statistics_are_not_the_commits(self):
        out = (
            "[main 1a2b3c4] tidy\n"
            "commit 0f1e2d3c4b5a69788796a5b4c3d2e1f0a9b8c7d6\n"
            " 9 files changed, 900 insertions(+), 90 deletions(-)\n"
        )
        row = _tool("git commit --allow-empty -m tidy && git show --stat HEAD~5", out)
        (event,) = uo.outcome_events([row])
        assert event[2:4] == (0, 0)

    def test_git_author_and_date_lines_before_the_statistics_are_skipped(self):
        out = (
            "[main 1a2b3c4] x\n Author: A <a@example.com>\n Date: Mon Nov 10 12:00:00 2026\n"
            " 1 file changed, 3 insertions(+), 1 deletion(-)\n"
        )
        (event,) = uo.outcome_events([_tool("git commit -m x", out)])
        assert event[2:4] == (3, 1)

    @pytest.mark.parametrize(
        "command, output",
        [
            ("git commit -m none", "On branch x\nnothing to commit, working tree clean\n"),
            ('git commit --amend -m "again"', COMMIT_OUT),
            ('echo "git commit is next"', "git commit is next\n"),
            ("git commit -m x", None),
            ("git commit -m x", 7),
            ("git log -3", "[feat/x 1a2b3c4] not a commit summary\n"),
        ],
    )
    def test_what_is_not_proven_is_not_counted(self, command, output):
        assert uo.outcome_events([_tool(command, output)]) == ()

    def test_an_unparseable_time_is_skipped_rather_than_dated_now(self):
        assert uo.outcome_events([_tool("git commit -m x", COMMIT_OUT, ts="yesterday")]) == ()

    def test_only_tool_rows_are_read(self):
        row = {**_tool("git commit -m x", COMMIT_OUT), "role": "assistant"}
        assert uo.outcome_events([row, "junk", None, {"role": "tool"}]) == ()

    def test_the_output_is_read_only_up_to_the_cap(self):
        padded = "x" * (uo._OUTPUT_READ_CHARS + 10) + "\n" + COMMIT_OUT
        assert uo.outcome_events([_tool("git commit -m x", padded)]) == ()


LOG_LINE = "1a2b3c4 add the thing\n"


class TestQuietCommitsAreNotCounted:
    """``git commit -q`` prints no summary line, so nothing in the row names its commit."""

    def test_a_quiet_commit_with_no_output_is_not_counted(self):
        # A call that failed or was cancelled without output is stored the same way.
        assert uo.outcome_events([_tool('git commit -q -m "x"', "")]) == ()

    @pytest.mark.parametrize("flags", ["-qm", "-aqm", "--quiet -m"])
    def test_a_quiet_commit_joined_to_a_log_is_not_counted(self, flags):
        row = _tool(f"git commit {flags} x && git log --oneline -1", LOG_LINE)
        assert uo.outcome_events([row]) == ()

    def test_a_failed_quiet_commit_does_not_count_an_earlier_log_line(self):
        # Nothing staged: the commit fails silently, and both lines name the old HEAD.
        command = (
            "git log --oneline -1; git commit -qm update >/dev/null 2>&1" " && git log --oneline -1"
        )
        row = _tool(command, "1a2b3c4 old\n1a2b3c4 old\n")
        assert uo.outcome_events([row]) == ()

    @pytest.mark.parametrize(
        "output",
        [
            "On branch main\nnothing to commit, working tree clean\n",
            "hook says no\n",
            "Aborting commit due to empty commit message.\n",
            "1a2b3c4 old\nerror: could not lock config file\n",
            "ok\n",
        ],
    )
    def test_any_output_without_a_summary_line_is_not_a_commit(self, output):
        assert uo.outcome_events([_tool("git commit -q -m x", output)]) == ()

    def test_a_call_that_has_not_returned_is_not_a_commit(self):
        row = _tool("git commit -q -m x && git log --oneline -1", LOG_LINE, done=False)
        assert uo.outcome_events([row]) == ()

    def test_a_commit_that_is_not_quiet_and_printed_nothing_is_not_counted(self):
        assert uo.outcome_events([_tool("git commit -m x > /dev/null", "")]) == ()

    def test_the_two_rows_of_one_tool_call_count_once(self):
        asked = _tool("git commit -m x", COMMIT_OUT, call_id="same")
        answered = {**asked, "content": "\u2705 git commit -m x"}
        out = uo.fold_outcome(
            uo.outcome_events([asked, answered]),
            start=0.0,
            end=math.inf,
            credits=1.0,
            merged_of=None,
        )
        assert out is not None and out["commits"] == 1


class TestTheCallsOwnCommand:
    """A long title can arrive cut short; the call's ``command`` argument is read instead."""

    def test_an_amend_past_the_end_of_a_cut_title_is_still_an_amend(self):
        row = _tool(
            'Running: git commit -m "a long subject ...',
            COMMIT_OUT,
            full_command='git commit -m "a long subject that runs on" --amend',
        )
        assert uo.outcome_events([row]) == ()

    def test_a_pull_request_create_is_read_from_the_whole_command(self):
        # The short title hides the ``;`` after which another command can print a URL.
        row = _tool(
            "gh pr create --title t",
            PR_URL + "\n",
            full_command="gh pr create --title t; gh pr view --json url",
        )
        assert uo.outcome_events([row]) == ()

    def test_a_title_that_describes_the_call_does_not_hide_its_commit(self):
        row = _tool("Save the work", COMMIT_OUT, full_command='git commit -m "add the thing"')
        assert _kinds(uo.outcome_events([row])) == ["commit"]

    @pytest.mark.parametrize("raw", ["not json", '{"command": ["git", "commit"]}', '{"path": "x"}'])
    def test_an_input_without_a_command_string_falls_back_to_the_title(self, raw):
        row = _tool('git commit -m "add the thing"', COMMIT_OUT)
        row["meta"]["input"] = raw
        assert _kinds(uo.outcome_events([row])) == ["commit"]


class TestCommitPreviews:
    """``--dry-run`` and the output formats git documents as implying it make no commit."""

    @pytest.mark.parametrize(
        "flags",
        [
            "--dry-run -qm preview >/dev/null",
            "--dry -q -m x",
            "--short -q -m x",
            "--porcelain -q -m x",
            "--porcelain=v1 -q -m x",
            "--long -q -m x",
            "-qz -m x",
            "--null -q -m x",
        ],
    )
    def test_a_quiet_preview_joined_to_a_log_is_not_a_commit(self, flags):
        # The log names the commit HEAD already was; the preview made none.
        row = _tool(f"git commit {flags} && git log --oneline -1", LOG_LINE)
        assert uo.outcome_events([row]) == ()

    @pytest.mark.parametrize("flags", ["--dry-run", "--short", "--porcelain", "--long", "-z"])
    def test_a_row_whose_only_commit_is_a_preview_counts_nothing(self, flags):
        assert uo.outcome_events([_tool(f"git commit {flags} -m x", COMMIT_OUT)]) == ()

    def test_a_real_commit_after_a_preview_still_counts(self):
        row = _tool("git commit --dry-run && git commit -m x", COMMIT_OUT)
        assert _kinds(uo.outcome_events([row])) == ["commit"]

    def test_a_preview_word_inside_the_message_is_not_an_option(self):
        row = _tool('git commit -m "drop --dry-run"', COMMIT_OUT)
        assert _kinds(uo.outcome_events([row])) == ["commit"]


class TestCommandScanner:
    """Commands are read by the shared shell reader, the batch guard's own."""

    def test_an_operator_inside_quotes_does_not_split_the_command(self):
        # Split at the quoted ``;``, the rest would read as an amend.
        row = _tool('git commit -m "a; git commit --amend"', COMMIT_OUT)
        assert _kinds(uo.outcome_events([row])) == ["commit"]

    @pytest.mark.parametrize("redirect", ["&>>log.txt", "2>&1", ">&2", "2>&1 | tee log.txt"])
    def test_a_redirection_is_not_a_join(self, redirect):
        row = _tool(f"gh pr create --title t --body b {redirect}", PR_URL + "\n")
        assert _kinds(uo.outcome_events([row])) == ["pr"]

    def test_a_command_with_a_quote_left_open_counts_nothing(self):
        # A title cut short: what follows the open quote cannot be read.
        assert uo.outcome_events([_tool('git commit -m "a long subject ...', COMMIT_OUT)]) == ()

    def test_git_options_before_the_subcommand_are_skipped(self):
        row = _tool("git -C /repo -c core.fsmonitor=false commit -m x", COMMIT_OUT)
        assert _kinds(uo.outcome_events([row])) == ["commit"]

    def test_an_abbreviated_amend_is_an_amend(self):
        assert uo.outcome_events([_tool("git commit --amen --no-edit", COMMIT_OUT)]) == ()

    def test_a_pull_request_body_with_operators_in_quotes_still_counts(self):
        row = _tool('gh pr create --title t --body "a; b || c"', PR_URL + "\n")
        assert _kinds(uo.outcome_events([row])) == ["pr"]

    def test_a_here_document_before_the_commit_is_data(self):
        # The apostrophe in the body is a character of the message file, not a quote.
        command = "cat > msg.txt <<'EOF'\nit's done; really\nEOF\ngit commit -F msg.txt"
        row = _tool(command, COMMIT_OUT)
        assert _kinds(uo.outcome_events([row])) == ["commit"]

    def test_a_pull_request_body_read_from_a_here_document_counts(self):
        command = "gh pr create --title t --body-file - <<'EOF'\nwon't && can't\nEOF\n"
        assert _kinds(uo.outcome_events([_tool(command, PR_URL + "\n")])) == ["pr"]


class TestPullRequests:
    def test_a_created_pull_request_is_counted_by_its_url(self):
        out = "Creating pull request for feat/x into main\n\n" + PR_URL + "\n"
        events = uo.outcome_events([_tool("gh pr create --title t --body b", out)])
        assert events == (("pr", EPOCH0, 0, 0, PR_URL),)

    def test_a_merge_request_is_counted_too(self):
        url = "https://gitlab.com/acme/widgets/-/merge_requests/9"
        events = uo.outcome_events([_tool("glab mr create --fill", url + "\n")])
        assert _kinds(events) == ["pr"]

    @pytest.mark.parametrize(
        "command, output",
        [
            ("gh pr create --title t", "pull request create failed: HTTP 502\n"),
            ("gh pr create --title t", "Warning: see " + PR_URL + " for the old one\n"),
            ("gh pr view 41", PR_URL + "\n"),
            ("gh pr create --title t", "https://github.com/acme/widgets/issues/7\n"),
            ("gh pr create --title t", "https://example.com/acme/widgets/pull/41\n"),
            (
                "gh pr create --title t",
                'a pull request for branch "feat/x" into branch "main" already exists:\n'
                + PR_URL
                + "\n",
            ),
        ],
    )
    def test_what_is_not_a_created_change_is_not_counted(self, command, output):
        assert uo.outcome_events([_tool(command, output)]) == ()

    @pytest.mark.parametrize(
        "command",
        [
            "gh pr create --fill 2>/dev/null || gh pr view --json url --jq .url",
            "gh pr create --fill; gh pr view --json url --jq .url",
            "gh pr view --json url --jq .url; gh pr create --fill",
            "gh pr create --fill\ngh pr list --json url --jq '.[0].url'",
        ],
    )
    def test_a_url_another_pull_request_call_may_have_printed_is_not_a_creation(self, command):
        assert uo.outcome_events([_tool(command, PR_URL + "\n")]) == ()

    def test_a_create_joined_by_and_or_piped_still_counts(self):
        row = _tool("git push -u origin feat/x && gh pr create --fill | tee pr.txt", PR_URL + "\n")
        assert _kinds(uo.outcome_events([row])) == ["pr"]


def _fold(events, *, start=0.0, end=math.inf, credits=12.0, merged_of=None):
    return uo.fold_outcome(events, start=start, end=end, credits=credits, merged_of=merged_of)


class TestFold:
    def _events(self):
        return uo.outcome_events(
            [
                _tool("git commit -m a", COMMIT_OUT, T0),
                _tool(
                    "git commit -m b", "[feat/x 2b3c4d5] b\n 1 file changed, 7 insertions(+)\n", T1
                ),
                _tool("gh pr create", PR_URL + "\n", T1),
            ]
        )

    def test_nothing_delivered_is_none(self):
        assert _fold(()) is None

    def test_commits_lines_and_pull_requests_sum(self):
        out = _fold(self._events())
        assert out == {"commits": 2, "lines_added": 17, "lines_removed": 3, "prs": 1}

    def test_the_window_bounds_what_counts_and_the_end_is_exclusive(self):
        first_end = EPOCH0 + 1
        assert _fold(self._events(), end=first_end)["commits"] == 1
        assert _fold(self._events(), start=EPOCH0 + 1)["commits"] == 1
        assert _fold(self._events(), end=EPOCH0) is None

    def test_the_same_commit_or_pull_request_twice_counts_once(self):
        events = self._events() + self._events()
        out = _fold(events)
        assert (out["commits"], out["prs"]) == (2, 1)

    def test_without_a_merge_source_the_merge_figures_are_absent(self):
        out = _fold(self._events())
        assert not {"merged", "unchecked", "per_merged"} & out.keys()

    def test_a_merged_pull_request_prices_the_session_per_merged_change(self):
        out = _fold(self._events(), credits=12.0, merged_of=lambda url: True)
        assert (out["merged"], out["unchecked"], out["per_merged"]) == (1, 0, 12.0)

    def test_an_unseen_pull_request_is_unchecked_not_open(self):
        out = _fold(self._events(), merged_of=lambda url: None)
        assert (out["merged"], out["unchecked"]) == (0, 1)
        assert "per_merged" not in out

    def test_an_open_pull_request_is_not_merged_and_not_unchecked(self):
        out = _fold(self._events(), merged_of=lambda url: False)
        assert (out["merged"], out["unchecked"]) == (0, 0)


def _cost(*slots: str) -> dict:
    return {
        "unit": "credits",
        "conversations": [{"slot": s, "credits": 10.0} for s in slots],
        "transcripts": {},
    }


def _run(cost, rows_by_key, *, stamp=1.0, merged_of=None, start=0.0, reads=None, clock=0):
    def read_rows(key):
        if reads is not None:
            reads.append(key)
        return rows_by_key.get(key), (key,)

    return uo.with_outcomes(
        cost,
        read_rows=read_rows,
        signature=lambda key: stamp,
        transcript_key=lambda slot: f"t-{slot}",
        merged_of=merged_of,
        start=start,
        now=lambda: float(clock),
    )


class TestWithOutcomes:
    ROWS = {"t-a": [_tool("git commit -m a", COMMIT_OUT), _tool("gh pr create", PR_URL + "\n")]}

    def test_a_delivering_conversation_gets_an_outcome_and_the_scan_is_named(self):
        out = _run(_cost("a", "b"), self.ROWS, merged_of=lambda url: True)
        first, second = out["conversations"]
        assert first["outcome"]["per_merged"] == 10.0
        assert "outcome" not in second
        assert out["outcome_scan"] == 2

    def test_the_memoised_payload_is_never_written_into(self):
        cost = _cost("a")
        out = _run(cost, self.ROWS)
        assert "outcome" not in cost["conversations"][0]
        assert "outcome_scan" not in cost
        assert out["conversations"][0] is not cost["conversations"][0]

    def test_a_window_with_no_delivery_returns_the_same_object(self):
        cost = _cost("a", "b")
        assert _run(cost, {}) is cost

    def test_an_empty_ranking_returns_the_same_object(self):
        cost = {"conversations": [], "transcripts": {}}
        assert _run(cost, self.ROWS) is cost

    def test_the_transcript_a_row_recorded_is_read_before_the_slot_name(self):
        cost = _cost("a")
        cost["transcripts"] = {"a": "linked-review"}
        out = _run(cost, {"linked-review": self.ROWS["t-a"]})
        assert out["conversations"][0]["outcome"]["commits"] == 1

    def test_an_unreadable_transcript_leaves_its_row_as_it_was(self):
        def read_rows(key):
            raise OSError("archive incomplete")

        cost = _cost("a")
        out = uo.with_outcomes(
            cost,
            read_rows=read_rows,
            signature=lambda key: 1.0,
            transcript_key=lambda slot: f"t-{slot}",
            merged_of=None,
            start=0.0,
        )
        assert out is cost

    def test_only_the_costliest_conversations_are_read(self):
        slots = [f"s{i}" for i in range(uo.OUTCOME_TOP_CONVOS + 5)]
        rows = {f"t-{s}": self.ROWS["t-a"] for s in slots}
        reads: list[str] = []
        out = _run(_cost(*slots), rows, reads=reads)
        assert len(reads) == uo.OUTCOME_TOP_CONVOS
        assert out["outcome_scan"] == uo.OUTCOME_TOP_CONVOS
        assert len(out["conversations"]) == len(slots)
        assert "outcome" not in out["conversations"][-1]

    def test_a_transcript_is_reread_only_when_its_file_changed(self):
        reads: list[str] = []
        _run(_cost("a"), self.ROWS, stamp=1.0, reads=reads, clock=0)
        _run(_cost("a"), self.ROWS, stamp=1.0, reads=reads, clock=600)
        assert reads == ["t-a"]
        _run(_cost("a"), self.ROWS, stamp=2.0, reads=reads, clock=600)
        assert reads == ["t-a", "t-a"]

    def test_a_live_transcript_is_reread_at_most_once_a_minute(self):
        # A live session's file changes between every poll of the Spend tab.
        reads: list[str] = []
        _run(_cost("a"), self.ROWS, stamp=1.0, reads=reads, clock=100)
        _run(_cost("a"), self.ROWS, stamp=2.0, reads=reads, clock=159)
        assert reads == ["t-a"]
        _run(_cost("a"), self.ROWS, stamp=3.0, reads=reads, clock=160)
        assert reads == ["t-a", "t-a"]

    def test_the_memo_holds_a_bounded_number_of_events(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(uo, "_MEMO_EVENTS_MAX", 2, raising=False)
        _floor_monkeypatch.setattr(uo, "_MEMO_EVENTS_TOTAL", 3, raising=False)
        commit = _tool("git commit -m a", COMMIT_OUT)
        rows = {"t-a": [commit, commit], "t-b": [commit, commit], "t-c": [commit] * 3}
        out = _run(_cost("a", "b", "c"), rows)
        # t-a left to keep the total under 3; t-c, over 2, is read again, never held.
        assert {key: len(entry[2]) for key, entry in uo._memo.items()} == {"t-b": 2}
        assert out["conversations"][2]["outcome"]["commits"] == 1

    def test_an_oversized_chain_is_never_memoised(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(uo, "_MEMO_CHAIN_MAX", 3, raising=False)
        chain = tuple(f"t-old-{n}" for n in range(3)) + ("t-a",)
        reads: list[str] = []

        def read_rows(key):
            reads.append(key)
            return self.ROWS["t-a"], chain

        for clock in (0.0, 1.0):
            out = uo.with_outcomes(
                _cost("a"),
                read_rows=read_rows,
                signature=lambda key: 1.0,
                transcript_key=lambda slot: f"t-{slot}",
                merged_of=None,
                start=0.0,
                now=lambda: clock,
            )
            assert out["conversations"][0]["outcome"]["commits"] == 1
            assert uo._memo == {}
        # Every poll reads it again through the seam.
        assert reads == ["t-a", "t-a"]

    def test_a_chain_within_the_cap_is_memoised_and_every_member_is_asked(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(uo, "_MEMO_CHAIN_MAX", 3, raising=False)
        chain = ("t-old-0", "t-old-1", "t-a")
        reads: list[str] = []
        asked: list[str] = []

        def read_rows(key):
            reads.append(key)
            return self.ROWS["t-a"], chain

        def withheld(key):
            asked.append(key)
            return False

        for clock in (0.0, 1.0):
            out = uo.with_outcomes(
                _cost("a"),
                read_rows=read_rows,
                signature=lambda key: 1.0,
                transcript_key=lambda slot: f"t-{slot}",
                merged_of=None,
                start=0.0,
                now=lambda: clock,
                withheld=withheld,
            )
            assert out["conversations"][0]["outcome"]["commits"] == 1
        assert reads == ["t-a"] and uo._memo["t-a"][3] == chain
        # The hit asked the requested transcript and then each older member.
        assert asked == ["t-a", "t-a", "t-old-0", "t-old-1"]

    def test_dollar_rows_round_to_cents_places(self):
        cost = _cost("a")
        cost["conversations"][0]["credits"] = 10.0 / 3
        out = uo.with_outcomes(
            cost,
            read_rows=lambda key: (self.ROWS["t-a"], (key,)),
            signature=lambda key: None,
            transcript_key=lambda slot: "t-a",
            merged_of=lambda url: True,
            start=0.0,
            places=4,
        )
        assert out["conversations"][0]["outcome"]["per_merged"] == 3.3333


class TestCachedMergeState:
    def test_the_chip_cache_answers_without_a_refresh(self, _floor_monkeypatch):
        _floor_monkeypatch.setitem(chip_status._check_cache, PR_URL, (0.0, {"state": "merged"}))
        _floor_monkeypatch.setitem(chip_status._check_cache, OTHER_URL, (0.0, {"state": "open"}))
        assert uo._cached_merge_state(PR_URL) is True
        assert uo._cached_merge_state(OTHER_URL) is False

    def test_a_url_the_cache_has_not_seen_is_unknown(self, _floor_monkeypatch):
        _floor_monkeypatch.delitem(chip_status._check_cache, PR_URL, raising=False)
        assert uo._cached_merge_state(PR_URL) is None


def _write_transcript(directory, slot: str, rows: list[dict]) -> ConversationLog:
    stem = slot_transcript_key(slot).replace(":", "_")
    lines = [json.dumps({"_type": "metadata", "created_at": T0})]
    lines += [json.dumps(r) for r in rows]
    (directory / f"{stem}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ConversationLog(directory)


def _write_chain(directory, rows_by_key: dict[str, list[dict]]) -> ConversationLog:
    """Transcripts that share one tab id, in the order given, read as one chain."""
    for n, (key, rows) in enumerate(rows_by_key.items()):
        meta = {"_type": "metadata", "created_at": f"2026-11-10T1{n}:00:00+00:00"}
        lines = [json.dumps({**meta, "tab_id": "tab-1"})] + [json.dumps(r) for r in rows]
        path = directory / f"{key.replace(':', '_')}.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ConversationLog(directory)


class _Request(dict):
    """The slice of an aiohttp request the owner gate and the handler read."""

    def __init__(self, log, user: str):
        super().__init__({"user": user, "app": ""})
        self.app = {"state": SimpleNamespace(conversation_log=log, owner_id="")}


def _request(log, *, owner: bool) -> _Request:
    return _Request(log, "local-app" if owner else "")


class TestAttachSessionOutcomes:
    WINDOW = TimeWindow(start=EPOCH0 - 86400, end=EPOCH0 + 7 * 86400, rolling=False)

    @pytest.mark.asyncio
    async def test_a_real_transcript_is_read_through_the_gateways_own_log(self, tmp_path):
        rows = [_tool("git commit -m a", COMMIT_OUT), _tool("gh pr create", PR_URL + "\n")]
        log = _write_transcript(tmp_path, "chat-1-1", rows)
        out = await uo.attach_session_outcomes(
            _request(log, owner=False), _cost("chat-1-1"), self.WINDOW
        )
        outcome = out["conversations"][0]["outcome"]
        assert (outcome["commits"], outcome["lines_added"], outcome["prs"]) == (1, 10, 1)
        # Merge state is the owner's: a caller without it gets counts only.
        assert "merged" not in outcome

    @pytest.mark.asyncio
    async def test_the_owner_is_also_given_the_cached_merge_state(
        self, tmp_path, _floor_monkeypatch
    ):
        _floor_monkeypatch.setitem(chip_status._check_cache, PR_URL, (0.0, {"state": "merged"}))
        rows = [_tool("gh pr create", PR_URL + "\n")]
        log = _write_transcript(tmp_path, "chat-1-1", rows)
        out = await uo.attach_session_outcomes(
            _request(log, owner=True), _cost("chat-1-1"), self.WINDOW
        )
        outcome = out["conversations"][0]["outcome"]
        assert (outcome["merged"], outcome["per_merged"]) == (1, 10.0)

    @pytest.mark.asyncio
    async def test_the_window_start_cuts_earlier_work_out(self, tmp_path):
        log = _write_transcript(tmp_path, "chat-1-1", [_tool("git commit -m a", COMMIT_OUT)])
        late = TimeWindow(start=EPOCH0 + 3600, end=EPOCH0 + 86400, rolling=False)
        cost = _cost("chat-1-1")
        out = await uo.attach_session_outcomes(_request(log, owner=False), cost, late)
        assert out is cost

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    async def test_a_restricted_transcript_gives_no_outcome(self, tmp_path, mode):
        rows = [_tool("git commit -m a", COMMIT_OUT), _tool("gh pr create", PR_URL + "\n")]
        log = _write_transcript(tmp_path, "chat-1-1", rows)
        await asyncio.to_thread(
            log.update_metadata, slot_transcript_key("chat-1-1"), {"memory_mode": mode}
        )
        cost = _cost("chat-1-1")
        assert (
            await uo.attach_session_outcomes(_request(log, owner=True), cost, self.WINDOW) is cost
        )

    @pytest.mark.asyncio
    async def test_a_session_tightened_after_it_was_read_loses_its_outcome(self, tmp_path):
        log = _write_transcript(tmp_path, "chat-1-1", [_tool("git commit -m a", COMMIT_OUT)])
        cost = _cost("chat-1-1")
        first = await uo.attach_session_outcomes(_request(log, owner=False), cost, self.WINDOW)
        assert first["conversations"][0]["outcome"]["commits"] == 1
        # Tightening the line keeps the file's mtime, so the memo is still current.
        await asyncio.to_thread(
            log.update_metadata, slot_transcript_key("chat-1-1"), {"memory_mode": "incognito"}
        )
        assert (
            await uo.attach_session_outcomes(_request(log, owner=False), cost, self.WINDOW) is cost
        )
        assert uo._memo == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    async def test_an_older_transcript_of_the_tab_tightened_after_a_read_takes_its_figures(
        self, tmp_path, mode
    ):
        older, newer = "dashboard:chat-0", "dashboard:chat-1"
        log = _write_chain(
            tmp_path,
            {
                older: [_tool("git commit -m a", COMMIT_OUT)],
                newer: [_tool("gh pr create", PR_URL + "\n")],
            },
        )
        assert log.chained_keys(newer) == [older, newer]
        cost = _cost("chat-1")
        cost["transcripts"] = {"chat-1": newer}
        first = await uo.attach_session_outcomes(_request(log, owner=False), cost, self.WINDOW)
        outcome = first["conversations"][0]["outcome"]
        assert (outcome["commits"], outcome["prs"]) == (1, 1)
        # The requested transcript's own line and mtime are unchanged, so its memo
        # entry is current; the older transcript's line now forbids its rows.
        await asyncio.to_thread(log.update_metadata, older, {"memory_mode": mode})
        assert "memory_mode" not in log.get_metadata(newer)
        assert (
            await uo.attach_session_outcomes(_request(log, owner=False), cost, self.WINDOW) is cost
        )
        assert uo._memo == {}

    def test_a_refused_read_drops_what_the_memo_held(self):
        rows = {"t-a": [_tool("git commit -m a", COMMIT_OUT)]}
        assert _run(_cost("a"), rows, stamp=1.0, clock=0)["conversations"][0]["outcome"]

        def refuse(key):
            raise TranscriptWithheld(key)

        cost = _cost("a")
        out = uo.with_outcomes(
            cost,
            read_rows=refuse,
            signature=lambda key: 2.0,
            transcript_key=lambda slot: f"t-{slot}",
            merged_of=None,
            start=0.0,
            now=lambda: 600.0,
        )
        assert out is cost and uo._memo == {}

    @pytest.mark.asyncio
    async def test_a_state_with_no_transcript_store_leaves_the_payload_alone(self):
        request = SimpleNamespace(app={"state": SimpleNamespace()})
        cost = _cost("chat-1-1")
        assert await uo.attach_session_outcomes(request, cost, self.WINDOW) is cost

    @pytest.mark.asyncio
    async def test_a_request_with_no_state_leaves_the_payload_alone(self):
        cost = _cost("chat-1-1")
        assert await uo.attach_session_outcomes(SimpleNamespace(app={}), cost, self.WINDOW) is cost


class TestRoute:
    @pytest.mark.asyncio
    async def test_the_startup_route_names_outcomes_and_titles_on_the_same_rows(
        self, tmp_path, _floor_monkeypatch
    ):
        rows = [_tool("git commit -m a", COMMIT_OUT), _tool("gh pr create", PR_URL + "\n")]
        log = _write_transcript(tmp_path, "chat-1-1", rows)
        cost = {
            "turns": 1,
            "unit": "credits",
            "conversations": [{"slot": "chat-1-1", "credits": 10.0}],
            "transcripts": {},
        }
        _floor_monkeypatch.setattr(
            h,
            "_telemetry_cfg",
            lambda: h._TelemetryState(
                enabled=True,
                directory=tmp_path,
                env_pinned=False,
                env_var=h.TELEMETRY_ENV_VAR,
                otlp_configured=False,
                retention_days=0,
            ),
        )
        _floor_monkeypatch.setattr(h, "_parse_startup_metrics", lambda _w, **_kw: {"other": []})
        _floor_monkeypatch.setattr(h, "_context_block", lambda _w: None)
        _floor_monkeypatch.setattr(h, "_cost_block", lambda _w: cost)
        app = web.Application()
        app["state"] = SimpleNamespace(
            conversation_log=log,
            owner_id="",
            get_slot=lambda key: SimpleNamespace(display_title="Billing audit"),
        )
        query = f"since={EPOCH0 - 86400}&until={EPOCH0 + 86400}"
        request = make_mocked_request("GET", f"/api/telemetry/startup?{query}", app=app)
        response = await h.api_telemetry_startup(request)
        row = json.loads(response.body)["cost"]["conversations"][0]
        assert row["title"] == "Billing audit"
        assert (row["outcome"]["commits"], row["outcome"]["prs"]) == (1, 1)


class TestPublicationRevalidation:
    """A session tightened after the scan, before the response is built, publishes nothing.

    The scan reads under the derivation seam and lets go of the transcript, and the
    handler then awaits the second unit's scan and both title passes. A ``!incognito``
    landing in that gap tightens the line, so the response revalidates each
    contributing chain through ``publication_hold`` before it is built.
    """

    def _route(self, tmp_path, _floor_monkeypatch, log, cost, tighten):
        _floor_monkeypatch.setattr(
            h,
            "_telemetry_cfg",
            lambda: h._TelemetryState(
                enabled=True,
                directory=tmp_path,
                env_pinned=False,
                env_var=h.TELEMETRY_ENV_VAR,
                otlp_configured=False,
                retention_days=0,
            ),
        )
        _floor_monkeypatch.setattr(h, "_parse_startup_metrics", lambda _w, **_kw: {"other": []})
        _floor_monkeypatch.setattr(h, "_context_block", lambda _w: None)
        _floor_monkeypatch.setattr(h, "_cost_block", lambda _w: cost)
        titles = h._with_conversation_titles
        calls = []

        async def titles_after_a_tightening(request, block):
            # The first title pass runs after both units were scanned.
            if not calls:
                await asyncio.to_thread(tighten)
            calls.append(block)
            return await titles(request, block)

        _floor_monkeypatch.setattr(h, "_with_conversation_titles", titles_after_a_tightening)
        app = web.Application()
        app["state"] = SimpleNamespace(
            conversation_log=log,
            owner_id="",
            get_slot=lambda key: SimpleNamespace(display_title="Billing audit"),
        )
        query = f"since={EPOCH0 - 86400}&until={EPOCH0 + 86400}"
        return make_mocked_request("GET", f"/api/telemetry/startup?{query}", app=app)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    async def test_a_tightening_after_the_scan_drops_the_outcome_from_both_units(
        self, tmp_path, _floor_monkeypatch, mode
    ):
        rows = [_tool("git commit -m a", COMMIT_OUT), _tool("gh pr create", PR_URL + "\n")]
        log = _write_transcript(tmp_path, "chat-1-1", rows)
        usd = {**_cost("chat-1-1"), "unit": "usd"}
        cost = {**_cost("chat-1-1"), "turns": 1, "usd": usd}
        key = slot_transcript_key("chat-1-1")
        request = self._route(
            tmp_path,
            _floor_monkeypatch,
            log,
            cost,
            lambda: log.update_metadata(key, {"memory_mode": mode}),
        )
        body = json.loads((await h.api_telemetry_startup(request)).body)["cost"]
        assert body["conversations"] == [
            {"slot": "chat-1-1", "credits": 10.0, "title": "Billing audit"}
        ]
        assert body["usd"]["conversations"] == [
            {"slot": "chat-1-1", "credits": 10.0, "title": "Billing audit"}
        ]

    @pytest.mark.asyncio
    async def test_an_older_transcript_of_the_tab_tightened_after_the_scan_is_caught(
        self, tmp_path, _floor_monkeypatch
    ):
        older, newer = "dashboard:chat-0", "dashboard:chat-1"
        log = _write_chain(
            tmp_path,
            {
                older: [_tool("git commit -m a", COMMIT_OUT)],
                newer: [_tool("gh pr create", PR_URL + "\n")],
            },
        )
        cost = {**_cost("chat-1"), "turns": 1, "transcripts": {"chat-1": newer}}
        request = self._route(
            tmp_path,
            _floor_monkeypatch,
            log,
            cost,
            lambda: log.update_metadata(older, {"memory_mode": "incognito"}),
        )
        body = json.loads((await h.api_telemetry_startup(request)).body)["cost"]
        assert "outcome" not in body["conversations"][0]

    @pytest.mark.asyncio
    async def test_an_untightened_session_keeps_its_outcome_in_both_units(
        self, tmp_path, _floor_monkeypatch
    ):
        log = _write_transcript(tmp_path, "chat-1-1", [_tool("git commit -m a", COMMIT_OUT)])
        usd = {**_cost("chat-1-1"), "unit": "usd"}
        cost = {**_cost("chat-1-1"), "turns": 1, "usd": usd}
        request = self._route(tmp_path, _floor_monkeypatch, log, cost, lambda: None)
        body = json.loads((await h.api_telemetry_startup(request)).body)["cost"]
        assert body["conversations"][0]["outcome"]["commits"] == 1
        assert body["usd"]["conversations"][0]["outcome"]["commits"] == 1

    @pytest.mark.asyncio
    async def test_outcomes_leave_in_the_same_response_build_as_the_titles(
        self, tmp_path, _floor_monkeypatch
    ):
        log = _write_transcript(tmp_path, "chat-1-1", [_tool("git commit -m a", COMMIT_OUT)])
        usd = {**_cost("chat-1-1"), "unit": "usd"}
        cost = {**_cost("chat-1-1"), "turns": 1, "usd": usd}
        request = self._route(tmp_path, _floor_monkeypatch, log, cost, lambda: None)
        order: list[str] = []
        sent: dict = {}
        titles = h._with_conversation_titles
        respond = h.respond_with_session_outcomes

        async def titled(request, block):
            order.append("titles")
            return await titles(request, block)

        async def built(request, payload, contributors):
            order.append("respond")
            sent["response"] = await respond(request, payload, contributors)
            return sent["response"]

        _floor_monkeypatch.setattr(h, "_with_conversation_titles", titled)
        _floor_monkeypatch.setattr(h, "respond_with_session_outcomes", built)
        response = await h.api_telemetry_startup(request)
        body = json.loads(response.body)["cost"]
        # Built after both title passes, and the response built under the holds is sent.
        assert order == ["titles", "titles", "respond"]
        assert response is sent["response"]
        assert body["conversations"][0]["title"] == "Billing audit"
        assert body["conversations"][0]["outcome"]["commits"] == 1

    @pytest.mark.asyncio
    async def test_a_checked_chain_stays_held_while_the_next_one_is_awaited(
        self, tmp_path, _floor_monkeypatch
    ):
        # Two sessions gave outcomes. While the response waits on the second one's
        # lock, a channel's ``!incognito`` tries to tighten the first. It must not
        # land before the response is built and still see the first session sent.
        _write_transcript(tmp_path, "chat-1-1", [_tool("git commit -m a", COMMIT_OUT)])
        log = _write_transcript(tmp_path, "chat-2-1", [_tool("git commit -m b", COMMIT_OUT)])
        first, second = slot_transcript_key("chat-1-1"), slot_transcript_key("chat-2-1")
        cost = {**_cost("chat-1-1", "chat-2-1"), "turns": 1}
        request = self._route(tmp_path, _floor_monkeypatch, log, cost, lambda: None)
        seen: dict = {}

        def tighten_first_if_free():
            # Takes the first transcript's locks only when no one holds them now.
            locks = [log._file_lock(stem) for stem in transcript_lock_stems(first)]
            taken = [lock for lock in locks if lock.acquire(blocking=False)]
            try:
                if len(taken) == len(locks):
                    log.update_metadata(first, {"memory_mode": "incognito"})
                    seen["tightened"] = True
            finally:
                for lock in taken:
                    lock.release()

        hold = log.publication_hold

        def hold_watching_the_second(key, **kwargs):
            if key == second and "tried" not in seen:
                seen["tried"] = True
                worker = threading.Thread(target=tighten_first_if_free)
                worker.start()
                worker.join()
            return hold(key, **kwargs)

        _floor_monkeypatch.setattr(log, "publication_hold", hold_watching_the_second)
        body = json.loads((await h.api_telemetry_startup(request)).body)["cost"]
        rows = {row["slot"]: row for row in body["conversations"]}
        assert seen.get("tried") is True
        assert not (seen.get("tightened") and "outcome" in rows["chat-1-1"])
        assert rows["chat-2-1"]["outcome"]["commits"] == 1

    @pytest.mark.asyncio
    async def test_more_chains_than_the_hold_bound_send_no_outcome(
        self, tmp_path, _floor_monkeypatch
    ):
        log = _write_transcript(tmp_path, "chat-1-1", [_tool("git commit -m a", COMMIT_OUT)])
        cost = {**_cost("chat-1-1"), "turns": 1}
        request = self._route(tmp_path, _floor_monkeypatch, log, cost, lambda: None)
        _floor_monkeypatch.setattr(uo, "_PUBLICATION_HOLD_MAX", 0)
        body = json.loads((await h.api_telemetry_startup(request)).body)["cost"]
        assert body["conversations"] == [
            {"slot": "chat-1-1", "credits": 10.0, "title": "Billing audit"}
        ]
