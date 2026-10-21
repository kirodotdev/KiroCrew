"""Unit tests for .github/scripts/republish_withheld_verdict.py.

The script re-runs the advisory review lane(s) whose withheld verdict never
reached a PR. These tests drive it with ``gh`` and the disposition gate replaced
by stubs, so the lane-location logic and the fail-closed behaviour are verified
without a live forge.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "republish_withheld_verdict.py"
OVERRIDE_HANDLER = REPO_ROOT / ".github" / "workflows" / "ai-review-human-override.yml"

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="requires the republisher script")


def _load():
    spec = importlib.util.spec_from_file_location("republish_withheld_verdict", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()

REPO = "kirodotdev/KiroCrew"
HEAD = "4328fd0f941f09ff10f245fbdb4accf7c246febe"

# A fork PR #7 from someone/fork:b, and the Fast Gate run its lanes bind to.
FORK_META = (True, "someone/fork", "b")
FAST_GATE_RUN = {
    "id": 900,
    "run_attempt": 1,
    "path": ".github/workflows/fast-gate.yml",
    "head_repository": {"full_name": "someone/fork"},
    "head_branch": "b",
}
FORK_EXTERNAL_ID = "ux-pr-7-900-1"


class _Recorder:
    """Replaces the module's gh / gate / rerun calls with scripted answers and
    records every rerun the script requested."""

    def __init__(
        self,
        monkeypatch,
        *,
        owed,
        same_repo_runs=None,
        fork_rows=None,
        fork_run_path=None,
        fork_run_attempt=1,
        pr_meta=(False, REPO, "feature-branch"),
    ):
        self.rerun_calls: list[str] = []
        self._owed = owed
        self._same_repo_runs = same_repo_runs or []
        self._fork_rows = fork_rows or []
        self._fork_run_path = fork_run_path
        self._fork_run_attempt = fork_run_attempt

        monkeypatch.setattr(MOD, "owed_lanes", lambda repo, pr, head: self._owed)
        monkeypatch.setattr(MOD, "_gh_json", self._gh_json)
        monkeypatch.setattr(MOD, "_rerun", self._rerun)
        # The PR's (is_fork, head repo, head ref), as _resolve_pr_meta reads it.
        monkeypatch.setattr(MOD, "_resolve_pr_meta", lambda repo, pr: pr_meta)

    def _rerun(self, repo, run_id):
        self.rerun_calls.append(run_id)
        return True

    def _gh_json(self, args):
        joined = " ".join(args)
        if "/actions/runs?" in joined:
            return {"workflow_runs": self._same_repo_runs}
        if "/check-runs?" in joined:
            return {"check_runs": self._fork_rows}
        if "/actions/runs/" in joined and self._fork_run_path is not None:
            return {"path": self._fork_run_path, "run_attempt": self._fork_run_attempt}
        return None


def test_no_owed_lane_reruns_nothing(monkeypatch, capsys):
    rec = _Recorder(monkeypatch, owed=[])
    assert MOD.republish(REPO, "7", HEAD) == 0
    assert rec.rerun_calls == []
    assert "no whole-design lane owes" in capsys.readouterr().out


def test_unreadable_gate_reruns_nothing(monkeypatch, capsys):
    rec = _Recorder(monkeypatch, owed=None)
    assert MOD.republish(REPO, "7", HEAD) == 0
    assert rec.rerun_calls == []
    assert "could not read this PR's comments" in capsys.readouterr().out


def test_same_repo_owed_lane_is_rerun_by_newest_matching_run(monkeypatch):
    runs = [
        {
            "id": 100,
            "path": ".github/workflows/design-review.yml",
            "head_repository": {"full_name": REPO},
            "head_branch": "feature-branch",
        },
        {
            "id": 200,  # newer, same lane/binding -> the one re-run
            "path": ".github/workflows/design-review.yml",
            "head_repository": {"full_name": REPO},
            "head_branch": "feature-branch",
        },
        {
            "id": 300,  # a different lane, must be ignored
            "path": ".github/workflows/ux-review.yml",
            "head_repository": {"full_name": REPO},
            "head_branch": "feature-branch",
        },
    ]
    rec = _Recorder(monkeypatch, owed=["DESIGN"], same_repo_runs=runs)
    assert MOD.republish(REPO, "7", HEAD) == 1
    assert rec.rerun_calls == ["200"]


def test_same_repo_binding_rejects_a_sibling_prs_run(monkeypatch, capsys):
    # Same head SHA, but a DIFFERENT head repo/branch -> not this PR's run.
    runs = [
        {
            "id": 200,
            "path": ".github/workflows/design-review.yml",
            "head_repository": {"full_name": "someone/fork"},
            "head_branch": "other",
        }
    ]
    rec = _Recorder(monkeypatch, owed=["DESIGN"], same_repo_runs=runs)
    assert MOD.republish(REPO, "7", HEAD) == 0
    assert rec.rerun_calls == []
    assert "could not be located" in capsys.readouterr().out


def test_fork_lane_is_rerun_from_its_lane_run_marker(monkeypatch):
    rows = [
        {
            "id": 10,
            "external_id": FORK_EXTERNAL_ID,
            "output": {"text": "Lane run: ...\n\n<!-- ai-review-fork-lane run=55501 -->"},
        }
    ]
    rec = _Recorder(
        monkeypatch,
        owed=["UX"],
        fork_rows=rows,
        same_repo_runs=[FAST_GATE_RUN],
        pr_meta=FORK_META,
        fork_run_path=".github/workflows/fork-ux-review.yml",
    )
    assert MOD.republish(REPO, "7", HEAD) == 1
    assert rec.rerun_calls == ["55501"]


def test_fork_lane_marker_pointing_at_the_wrong_workflow_is_refused(monkeypatch, capsys):
    rows = [
        {
            "id": 10,
            "external_id": FORK_EXTERNAL_ID,
            "output": {"text": "<!-- ai-review-fork-lane run=55501 -->"},
        }
    ]
    rec = _Recorder(
        monkeypatch,
        owed=["UX"],
        fork_rows=rows,
        same_repo_runs=[FAST_GATE_RUN],
        pr_meta=FORK_META,
        fork_run_path=".github/workflows/some-other.yml",  # not the fork UX lane
    )
    assert MOD.republish(REPO, "7", HEAD) == 0
    assert rec.rerun_calls == []
    assert "could not be located" in capsys.readouterr().out


def test_fork_binding_rejects_a_sibling_prs_row_on_a_shared_head(monkeypatch, capsys):
    """Two fork PRs can share one head SHA. The sibling's check-run row is the
    NEWEST by id, but its external_id names PR #8, so PR #7's re-run must not
    pick it. Fails when the external_id binding is removed."""
    rows = [
        {
            "id": 10,
            "external_id": FORK_EXTERNAL_ID,
            "output": {"text": "<!-- ai-review-fork-lane run=55501 -->"},
        },
        {
            "id": 20,  # newer, but the sibling PR #8's lane run
            "external_id": "ux-pr-8-901-1",
            "output": {"text": "<!-- ai-review-fork-lane run=66601 -->"},
        },
    ]
    rec = _Recorder(
        monkeypatch,
        owed=["UX"],
        fork_rows=rows,
        same_repo_runs=[FAST_GATE_RUN],
        pr_meta=FORK_META,
        fork_run_path=".github/workflows/fork-ux-review.yml",
    )
    assert MOD.republish(REPO, "7", HEAD) == 1
    assert rec.rerun_calls == ["55501"]


def test_fork_with_only_a_sibling_prs_row_is_not_rerun(monkeypatch, capsys):
    rows = [
        {
            "id": 20,
            "external_id": "ux-pr-8-901-1",
            "output": {"text": "<!-- ai-review-fork-lane run=66601 -->"},
        }
    ]
    rec = _Recorder(
        monkeypatch,
        owed=["UX"],
        fork_rows=rows,
        same_repo_runs=[FAST_GATE_RUN],
        pr_meta=FORK_META,
        fork_run_path=".github/workflows/fork-ux-review.yml",
    )
    assert MOD.republish(REPO, "7", HEAD) == 0
    assert rec.rerun_calls == []
    assert "could not be located" in capsys.readouterr().out


def test_deleted_fork_binds_no_lane_run(monkeypatch, capsys):
    """A deleted fork has no head repository, so no Fast Gate run binds to it
    and nothing is re-run -- matching an empty name would bind runs readiness
    never reads."""
    rows = [
        {
            "id": 10,
            "external_id": FORK_EXTERNAL_ID,
            "output": {"text": "<!-- ai-review-fork-lane run=55501 -->"},
        }
    ]
    rec = _Recorder(
        monkeypatch,
        owed=["UX"],
        fork_rows=rows,
        same_repo_runs=[dict(FAST_GATE_RUN, head_repository=None)],
        pr_meta=(True, "", "b"),
        fork_run_path=".github/workflows/fork-ux-review.yml",
    )
    assert MOD.republish(REPO, "7", HEAD) == 0
    assert rec.rerun_calls == []


def test_same_repo_run_at_the_attempt_cap_is_not_rerun(monkeypatch, capsys):
    """The blocking finding: a lane whose withhold cause persists must stop being
    re-run once it has reached MAX_RERUN_ATTEMPTS, so the sweep cannot re-fire a
    paid model review every STALE_MINUTES forever. Fails if the cap is removed."""
    runs = [
        {
            "id": 200,
            "run_attempt": MOD.MAX_RERUN_ATTEMPTS,  # already at the cap
            "path": ".github/workflows/design-review.yml",
            "head_repository": {"full_name": REPO},
            "head_branch": "feature-branch",
        }
    ]
    rec = _Recorder(monkeypatch, owed=["DESIGN"], same_repo_runs=runs)
    assert MOD.republish(REPO, "7", HEAD) == 0
    assert rec.rerun_calls == []
    out = capsys.readouterr().out
    assert "attempts without publishing" in out
    assert "manually" in out


def test_same_repo_run_below_the_cap_is_still_rerun(monkeypatch):
    """One attempt below the cap must still re-run -- the bound stops a stuck
    lane, it does not stop the recovery itself."""
    runs = [
        {
            "id": 200,
            "run_attempt": MOD.MAX_RERUN_ATTEMPTS - 1,
            "path": ".github/workflows/design-review.yml",
            "head_repository": {"full_name": REPO},
            "head_branch": "feature-branch",
        }
    ]
    rec = _Recorder(monkeypatch, owed=["DESIGN"], same_repo_runs=runs)
    assert MOD.republish(REPO, "7", HEAD) == 1
    assert rec.rerun_calls == ["200"]


def test_fork_run_at_the_attempt_cap_is_not_rerun(monkeypatch, capsys):
    """The attempt bound holds on the fork path too, read from the same
    actions/runs/{id} payload the fork-workflow verification already fetches."""
    rows = [
        {
            "id": 10,
            "external_id": FORK_EXTERNAL_ID,
            "output": {"text": "<!-- ai-review-fork-lane run=55501 -->"},
        }
    ]
    rec = _Recorder(
        monkeypatch,
        owed=["UX"],
        fork_rows=rows,
        same_repo_runs=[FAST_GATE_RUN],
        pr_meta=FORK_META,
        fork_run_path=".github/workflows/fork-ux-review.yml",
        fork_run_attempt=MOD.MAX_RERUN_ATTEMPTS,
    )
    assert MOD.republish(REPO, "7", HEAD) == 0
    assert rec.rerun_calls == []
    assert "attempts without publishing" in capsys.readouterr().out


def test_same_repo_binding_rejects_a_run_in_a_sibling_head_repo(monkeypatch, capsys):
    """The head_repository.full_name binding must reject a run that matches the
    workflow, head SHA and branch but belongs to a DIFFERENT head repository
    (the shared-fork-head case). Fails when that binding is removed."""
    runs = [
        {
            "id": 200,
            "run_attempt": 1,
            "path": ".github/workflows/design-review.yml",
            # Same branch name and lane, but a different head repo -> not ours.
            "head_repository": {"full_name": "someone/fork"},
            "head_branch": "feature-branch",
        }
    ]
    rec = _Recorder(monkeypatch, owed=["DESIGN"], same_repo_runs=runs)
    assert MOD.republish(REPO, "7", HEAD) == 0
    assert rec.rerun_calls == []
    assert "could not be located" in capsys.readouterr().out


def test_owed_lanes_drops_names_this_script_cannot_rerun(monkeypatch):
    """Only lanes in LANES are returned; a required lane the predicate might name
    is not silently treated as handled here."""

    class _Proc:
        returncode = 0
        stdout = json.dumps({"ok": True, "unpublished": ["DESIGN", "OPUS"]})

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    assert MOD.owed_lanes(REPO, "7", HEAD) == ["DESIGN"]


def test_owed_lanes_fails_closed_when_the_gate_is_not_ok(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = json.dumps({"ok": False, "unpublished": []})

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    assert MOD.owed_lanes(REPO, "7", HEAD) is None


def _hang(*args, **kwargs):
    """A subprocess.run stand-in that times out -- and refuses to run at all
    without a timeout, which is the hang the bound exists to prevent."""
    timeout = kwargs.get("timeout")
    assert timeout is not None and timeout > 0, "subprocess.run called with no timeout"
    raise subprocess.TimeoutExpired(args[0], timeout)


def test_gh_read_that_hangs_is_a_failed_read(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _hang)
    assert MOD._gh_json(["api", f"repos/{REPO}/pulls/7"]) is None


def test_disposition_gate_that_hangs_fails_closed(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _hang)
    assert MOD.owed_lanes(REPO, "7", HEAD) is None


def test_rerun_that_hangs_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _hang)
    assert MOD._rerun(REPO, "55501") is False


def test_the_lane_table_matches_the_override_handler() -> None:
    """The lane -> workflow mapping is duplicated from ai-review-human-override.yml;
    pin the two so a lane rename cannot drift this script's re-run target."""
    if not OVERRIDE_HANDLER.exists():
        pytest.skip("requires the override handler workflow")
    handler = OVERRIDE_HANDLER.read_text(encoding="utf-8")
    expected = {
        "DESIGN": ("Design Review", "design-review.yml", "fork-design-review.yml"),
        "UX": ("UX Review", "ux-review.yml", "fork-ux-review.yml"),
        "FIRST-PRINCIPLES": (
            "First Principles Review",
            "first-principles-review.yml",
            "fork-first-principles-review.yml",
        ),
    }
    for name, (check_name, same_repo, fork) in expected.items():
        spec = MOD.LANES[name]
        assert spec["check_name"] == check_name
        assert spec["same_repo"] == same_repo
        assert spec["fork"] == fork
        # The handler re-runs the same check name and fork workflow for this lane.
        assert check_name in handler
        assert same_repo in handler
        assert fork in handler
        # The fork lane stamps its check-run with the external_id prefix the
        # re-run binds on; a rename there must fail here, not strand the lane.
        fork_text = (REPO_ROOT / ".github" / "workflows" / fork).read_text(encoding="utf-8")
        assert f'external_id="{spec["fork_lane"]}-pr-$PR-$WR_RUN_ID-$WR_RUN_ATTEMPT"' in fork_text
    # And the three advisory lanes are the whole set this script re-runs: the
    # required lanes (Opus/GPT/Security Scope) fail CLOSED on a withheld verdict
    # and are recovered by their own finalize, never by this path.
    assert set(MOD.LANES) == set(expected)
