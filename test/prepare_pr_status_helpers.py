"""Shared loaders and fakes for the kirocrew-prepare-pr ``pr_status.py`` suites."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

from skill_script_helpers import is_rollup_graphql_read, load_skill_script, rollup_graphql_response

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-prepare-pr"
    / "scripts"
    / "pr_status.py"
)


def _load_script() -> ModuleType:
    return load_skill_script("prepare_pr_status", SCRIPT)


def _pr_payload(checks: list[dict[str, str]], **overrides: object) -> str:
    payload: dict[str, object] = {
        "number": 42,
        "title": "fix: keep the change focused",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "REVIEW_REQUIRED",
        "url": "https://github.com/example/repo/pull/42",
        "headRefName": "fix/focused",
        "statusCheckRollup": checks,
        # A resolved issue link, so unrelated tests do not emit the advisory
        # NOTICE line. It is NOT a CLEAN precondition -- the issue-link check
        # never changes the exit code. Tests that exercise it override these.
        "body": "Fixes #7",
        "closingIssuesReferences": [{"number": 7}],
        "headRefOid": "f" * 40,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _fake_git(args: list[str]) -> tuple[int, str, str]:
    """Answer the git commands the embedded green-age probe issues.

    A fresh verdict by construction: the base is reported as having moved in
    nothing. Tests that need a STALE probe pass their own ``moved``/``mine``.
    """
    return _fake_git_with(args, moved=[], mine=[])


def _fake_git_with(
    args: list[str], moved: list[str], mine: list[str], commits: int = 0
) -> tuple[int, str, str]:
    rest = args[1:]
    if rest[:1] == ["fetch"]:
        return 0, "", ""
    if rest[:2] == ["rev-parse", "--is-inside-work-tree"]:
        return 0, "true", ""
    if rest[:1] == ["rev-parse"]:
        return 0, "a" * 40, ""
    if rest[:1] == ["merge-base"]:
        return 0, "b" * 40, ""
    if rest[:2] == ["rev-list", "--count"]:
        return 0, str(commits), ""
    if rest[:2] == ["diff", "--name-only"]:
        # Two-dot compares the tested base with the base tip (what main gained);
        # three-dot compares the base with this head (what the branch owns).
        return 0, "\n".join(mine if "..." in rest[-1] else moved), ""
    if rest[:1] == ["show"]:
        return 0, "", ""
    raise AssertionError("unexpected git command: {}".format(args))


def _install_fake_gh(
    module: ModuleType,
    payload: str,
    comments: str = "[]",
    head_run_events: list[str] | None = None,
    permissions: dict[str, str] | None = None,
    git: object = None,
    pr_files: list[str] | None = None,
) -> None:
    events = ["pull_request"] if head_run_events is None else head_run_events
    fake_git = git or _fake_git

    def fake_run(args: list[str]) -> tuple[int, str, str]:
        if args[:1] == ["git"]:
            return fake_git(args)  # type: ignore[operator]
        if args[:3] == ["gh", "auth", "status"]:
            return 0, "", ""
        # The green-age probe's own query, which asks for `files` and nothing else.
        if args[:3] == ["gh", "pr", "view"] and "files" in args:
            return 0, "\n".join(pr_files or []), ""
        if args[:3] == ["gh", "pr", "view"]:
            return 0, payload, ""
        if args[:3] == ["gh", "repo", "view"]:
            return 0, "example/repo", ""
        # The rollup is read through GraphQL, in its own call, because the run
        # fields the collapse keys on are not in `gh pr view --json`'s rows. The
        # fake answers from the same flat fixtures the payload carries.
        if is_rollup_graphql_read(args):
            core = json.loads(payload)
            checks = core.get("statusCheckRollup") or []
            return 0, rollup_graphql_response(checks, core.get("headRefOid") or ""), ""
        if args[:2] == ["gh", "api"] and "/collaborators/" in args[2]:
            if permissions is None:
                raise AssertionError("unexpected command: {}".format(args))
            login = args[2].split("/")[4]
            return 0, json.dumps({"permission": permissions.get(login, "none")}), ""
        if args[:2] == ["gh", "api"] and "/issues/" in args[2] and "/comments" in args[2]:
            return 0, comments, ""
        if args[:2] == ["gh", "api"] and "/actions/runs" in args[2]:
            runs = [{"event": e} for e in events]
            return 0, json.dumps({"total_count": len(runs), "workflow_runs": runs}), ""
        # The supersession gate's cheap probe: how many bodies has this comment
        # held? One means the current body IS the only body, so nothing could have
        # been superseded and the expensive body-bearing read is never made. A stub
        # that refused this would make every lane's history UNREADABLE, which the
        # local gate correctly fails closed on.
        if args[:3] == ["gh", "api", "graphql"]:
            edits = {"totalCount": 1, "pageInfo": {"hasNextPage": False}, "nodes": []}
            return 0, json.dumps({"data": {"node": {"userContentEdits": edits}}}), ""
        raise AssertionError("unexpected command: {}".format(args))

    module.run = fake_run
    module.unresolved_thread_count = lambda _number: 3


def _last_line_json(capsys) -> dict:
    """Parse the --json object, which is contracted to be the LAST stdout line."""
    lines = [ln for ln in capsys.readouterr().out.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1])


_HEAD = "f" * 40
_OLD = "a" * 40


def _bot_comment(
    body: str,
    user_type: str = "Bot",
    login: str = "github-actions[bot]",
    key: str | None = "codex-ai-review",
) -> dict[str, object]:
    prefix = f"<!-- {key} -->\n" if key else ""
    # node_id is what the supersession reader needs to ask how many bodies this
    # comment has held. Every real comment from the API carries one; a fixture
    # without it makes the stored history UNREADABLE, which the local gate
    # correctly fails closed on.
    return {
        "node_id": "IC_" + str(abs(hash(prefix + body)) % 10**9),
        "user": {"type": user_type, "login": login},
        "body": prefix + body,
    }


def _clean_checks() -> list[dict[str, str]]:
    return [{"context": "PR Readiness", "state": "SUCCESS"}]


# A realistic head: the all-`f` fixture cannot exercise elision, because any
# splice of it is also a prefix of it.
_MIXED_HEAD = "db7c4361f0a92be5147c3d8e6b0af215934cde78"
# The shape the Design lane emits: the head's first 14
# characters spliced to its last 11, middle dropped, 25 characters total.
_ELIDED = _MIXED_HEAD[:14] + _MIXED_HEAD[-11:]


_GREEN_CHECKS = [{"context": "PR Readiness", "state": "SUCCESS"}]


def _disposition(author: str, target: str, body_tail: str, comment_id: int = 11) -> dict:
    return {
        "id": comment_id,
        "user": {"type": "User", "login": author},
        "body": (
            "<!-- ai-review-disposition target="
            + target
            + " head="
            + "f" * 40
            + " -->\n"
            + body_tail
        ),
    }


_GATE_HEAD = "f" * 40


def _gate_bot_comment(head: str = _GATE_HEAD) -> dict:
    return {
        # Real comments carry a node id; without one the supersession reader
        # cannot address the stored history and fails closed.
        "node_id": "IC_fixture",
        "user": {"type": "Bot", "login": "github-actions[bot]"},
        "body": (
            "<!-- codex-ai-review -->\n"
            "BLOCKING -- src/x.py:10 -- tighten -> Fix: x\n"
            "[GPT-REVIEWED] " + head
        ),
    }


def _gate_argv() -> list[str]:
    return [
        "pr_status.py",
        "--disposition-gate",
        "--repo",
        "example/repo",
        "--pr",
        "42",
        "--head",
        _GATE_HEAD,
    ]
