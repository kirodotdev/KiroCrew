"""Fixtures and fabricated forge payloads shared by the claim preflight tests.

``test_pipeline_conductor_claim_preflight.py`` and its ``_check_detectors`` and
``_end_to_end`` siblings import these by name: ``mod`` loads the script under test,
``clean_checks`` fabricates a check set, ``an_issue``, ``a_comment`` and ``a_pull``
fabricate the payloads the forge returns, and ``Forge`` stands in for the script's
``run``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "pipeline-conductor"
    / "scripts"
    / "claim_preflight.py"
)

REPO = "kirodotdev/KiroCrew"
ITEM = 8029


@pytest.fixture(name="mod")
def mod_fixture():
    return load_skill_script("claim_preflight", SCRIPT)


def naive_claim(checks: dict) -> bool:
    """The predicate this script replaces: ONE question, and an empty answer
    read as permission.

    The old skill line was `gh pr list --search ... --state open`, so it could
    see exactly one thing: a same-repo OPEN pull request. It could not see a
    merged PR (wrong state), a fork PR (the search missed them, and the single
    field it fell back on measured empty), a claim written in prose, or a symbol
    that is not on the base.

    Every branch test asserts this returns True, i.e. the old predicate would
    have burned a dispatch on the item.
    """
    hits = checks.get("open_prs")
    if not isinstance(hits, list):
        return True
    return not [hit for hit in hits if isinstance(hit, dict) and not hit.get("is_cross_repository")]


# --------------------------------------------------------------------------- #
# fixtures for fabricated checks
# --------------------------------------------------------------------------- #


def clean_checks(**overrides) -> dict:
    checks = {
        "open_prs": [],
        "merged_prs": [],
        "prose_claim": {
            "closure_requested": False,
            "claimed_by_other": False,
            "claim_without_standing": False,
            "claimed_by": None,
            "where": None,
            "comment_id": None,
            "pattern": None,
        },
        "symbol_on_base": {
            "symbols": [],
            "present": [],
            "missing": [],
            "bug_class": False,
            "bug_class_by": None,
        },
        "recency": {"age_days": 200, "author_association": "NONE", "risk": "low"},
        "forge_claim": {"label": None, "assignees": [], "foreign": False},
    }
    checks.update(overrides)
    return checks


def an_issue(**overrides) -> dict:
    issue = {
        "number": ITEM,
        "title": "the probe cannot tell a finished worker from a wedged one",
        "body": "The handled set keeps one entry per key.",
        "user": {"login": "reporter"},
        "created_at": "2026-01-01T00:00:00Z",
        "author_association": "NONE",
        "labels": [],
        "type": None,
    }
    issue.update(overrides)
    return issue


def a_comment(
    body: str,
    *,
    login="reporter",
    ident=123456,
    kind="User",
    created="2026-09-02T00:00:00Z",
    association=None,
) -> dict:
    """A comment payload. ``association`` defaults to OWNER only so the many
    tests that just need SOME closure request keep standing; the standing rule
    itself is pinned by its own tests, which pass it explicitly.
    """
    return {
        "id": ident,
        "body": body,
        "user": {"login": login, "type": kind},
        "created_at": created,
        "author_association": "OWNER" if association is None else association,
    }


def a_pull(
    number: int,
    *,
    state: str = "open",
    merged: bool = False,
    sha: str | None = None,
    head: str | None = REPO,
    user: str = "someone",
    draft: bool = False,
    closes: int | None = ITEM,
    association: str = "NONE",
) -> dict:
    """A pull payload. ``closes`` puts a closing keyword for that item in the
    body (None for a bare mention), because a merged PR is coverage only if it
    claims to close the item. ``association`` is the author's standing, which
    decides whether a FORK PR's SKIP is marked unvouched."""
    return {
        "number": number,
        "state": state,
        "merged": merged,
        "merge_commit_sha": sha,
        "draft": draft,
        "title": f"fix something in {number}",
        "body": (f"Fixes #{closes}" if closes is not None else f"Related to #{ITEM}"),
        "user": {"login": user},
        "author_association": association,
        "head": {"repo": {"full_name": head} if head else None},
        "base": {"repo": {"full_name": REPO}},
    }


class Forge:
    """A stand-in for the module's ``run``: routes an argv to canned output.

    Records every call so a test can assert on what the script asked, including
    that it never asked for a write.
    """

    def __init__(
        self,
        *,
        timeline: list | None = None,
        pulls: dict | None = None,
        issue: dict | None = None,
        comments: list | None = None,
        login: str = "us",
        failures: dict | None = None,
        git_rc: dict | None = None,
    ):
        self.timeline = timeline if timeline is not None else []
        self.pulls = pulls or {}
        self.issue = issue if issue is not None else an_issue()
        self.comments = comments if comments is not None else []
        self.login = login
        self.failures = failures or {}
        self.git_rc = git_rc or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd=None):
        self.calls.append(list(argv))
        if argv[0] == "git":
            # ``rev-parse --git-dir`` is the --repo-dir validation and
            # ``rev-parse --verify`` the default-branch check: two different
            # questions, so a test can fail one without failing the other.
            verb = "git-dir" if "--git-dir" in argv else argv[3]
            return self.git_rc.get(verb, 0), "", ""
        target = argv[2] if len(argv) > 2 else ""
        for needle, slug in self.failures.items():
            if needle in " ".join(argv):
                return 1, "", slug
        if "/timeline" in target:
            return 0, json.dumps(self.timeline), ""
        if "/pulls/" in target:
            number = int(target.rsplit("/", 1)[1])
            return 0, json.dumps(self.pulls[number]), ""
        if "/comments" in target:
            return 0, json.dumps(self.comments), ""
        if target == "user":
            return 0, json.dumps({"login": self.login}), ""
        if target.startswith("repos/") and "/issues/" in target:
            return 0, json.dumps(self.issue), ""
        raise AssertionError(f"unrouted argv: {argv}")  # pragma: no cover


def run_main(mod, monkeypatch, forge: Forge, extra: list[str] | None = None) -> int:
    monkeypatch.setattr(mod, "run", forge)
    return mod.main(["--repo", REPO, "--item", str(ITEM), *(extra or [])])
