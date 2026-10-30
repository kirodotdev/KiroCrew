"""``fix_loop_metrics.py``: the weekly counters over ``/ai-review override`` records."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
METRICS_SCRIPT = ROOT / ".github" / "scripts" / "fix_loop_metrics.py"
ANALYSIS = ROOT / ".github" / "workflows" / "fix-loop-analysis.yml"
CONTRACT = (
    ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-prepare-pr"
    / "scripts"
    / "_review_contract.py"
)
SINCE = "2026-10-14T00:00:00Z"
HEAD = "a" * 40


@pytest.fixture(scope="module")
def mod():
    return load_skill_script("fix_loop_metrics_overrides", METRICS_SCRIPT)


def marker(actor: str = "writer", source: int = 1) -> str:
    return f"<!-- ai-review-human-override target=gpt head={HEAD} actor={actor} source={source} -->"


def record(cls: str, issue: str = "none", author: str = "false") -> str:
    return f"<!-- ai-review-override-record class={cls} issue={issue} author_is_actor={author} -->"


def comment(
    body: str,
    *,
    login: str = "github-actions[bot]",
    created: str = "2026-10-15T12:00:00Z",
    pr: int = 7,
) -> dict:
    return {
        "user": {"login": login},
        "body": body,
        "created_at": created,
        "html_url": f"https://github.com/o/r/pull/{pr}#issuecomment-1",
        "issue_url": f"https://api.github.com/repos/o/r/issues/{pr}",
    }


def classed(cls: str, *, issue: str = "none", author: str = "false", **kw) -> dict:
    body = f"{marker()}\n{record(cls, issue, author)}\n## Human judgment recorded\n\n> why"
    return comment(body, **kw)


@pytest.fixture
def api(mod, monkeypatch):
    """Serve ``comments`` as the paginated issue-comments read; return the calls made."""
    calls: list[list[str]] = []

    def serve(comments, shape="merged"):
        def fake_run(argv, *, check=True):
            calls.append(argv)
            if shape == "pages":
                return "".join(json.dumps(comments[i : i + 2]) for i in range(0, len(comments), 2))
            return json.dumps(comments)

        monkeypatch.setattr(mod, "run", fake_run)
        return calls

    return serve


class TestOverrideCounters:
    def test_every_counter_is_reported(self, mod, api) -> None:
        api(
            [
                classed("false-positive"),
                classed("false-positive", author="true"),
                classed("deferred", issue="1234"),
                classed("deferred", issue="none"),
                classed("owner-decision", author="true", pr=9),
                comment(f"{marker()}\n## Human judgment recorded\n\n> old record"),
            ]
        )

        out = mod.override_metrics("o/r", SINCE)

        assert out["overrides_total"] == 6
        assert out["overrides_by_class"] == {
            "deferred": 2,
            "false-positive": 2,
            "owner-decision": 1,
            "unclassified": 1,
        }
        assert out["overrides_self_authored"] == 2
        assert out["overrides_deferred_without_issue"] == 1
        assert out["owner_decisions"] == [
            {
                "pr": "#9",
                "actor": "writer",
                "url": "https://github.com/o/r/pull/9#issuecomment-1",
            }
        ]

    def test_an_empty_window_is_a_real_zero(self, mod, api) -> None:
        api([])

        assert mod.override_metrics("o/r", SINCE) == {
            "overrides_total": 0,
            "overrides_by_class": {},
            "overrides_self_authored": 0,
            "overrides_deferred_without_issue": 0,
            "owner_decisions": [],
        }

    def test_a_person_pasting_the_marker_is_not_counted(self, mod, api) -> None:
        api([classed("false-positive", login="someone"), classed("false-positive")])

        assert mod.override_metrics("o/r", SINCE)["overrides_total"] == 1

    def test_comments_that_are_not_records_are_ignored(self, mod, api) -> None:
        api(
            [
                comment("/ai-review override gpt " + HEAD + " false-positive: not the record"),
                comment("## GPT 6.1 Review\n" + marker()),
                comment("<!-- ai-review-disposition target=gpt head=abc -->"),
            ]
        )

        assert mod.override_metrics("o/r", SINCE)["overrides_total"] == 0

    def test_the_window_is_on_creation_not_on_last_update(self, mod, api) -> None:
        # The API's `since` selects on the last update, so an old record that was
        # edited this week comes back and must not count as a new override.
        api([classed("deferred", issue="5", created="2026-09-01T00:00:00Z"), classed("deferred")])

        out = mod.override_metrics("o/r", SINCE)

        assert out["overrides_total"] == 1 and out["overrides_deferred_without_issue"] == 1

    def test_a_malformed_class_line_reads_as_unclassified(self, mod, api) -> None:
        bad = (
            f"{marker()}\n<!-- ai-review-override-record class=x issue=y author_is_actor=maybe -->"
        )
        api([comment(bad)])

        assert mod.override_metrics("o/r", SINCE)["overrides_by_class"] == {"unclassified": 1}

    def test_the_class_line_must_be_line_two(self, mod, api) -> None:
        late = f"{marker()}\n## Human judgment recorded\n{record('deferred', '5')}"
        api([comment(late)])

        assert mod.override_metrics("o/r", SINCE)["overrides_by_class"] == {"unclassified": 1}

    @pytest.mark.parametrize("shape", ["merged", "pages"])
    def test_paginated_output_parses_either_way(self, mod, api, shape: str) -> None:
        api([classed("not-applicable")] * 5, shape)

        assert mod.override_metrics("o/r", SINCE)["overrides_total"] == 5

    def test_the_read_is_one_paginated_call_from_the_window_start(self, mod, api) -> None:
        calls = api([])
        mod.override_metrics("o/r", SINCE)

        assert len(calls) == 1
        assert calls[0][:2] == ["gh", "api"] and "--paginate" in calls[0]
        assert f"repos/o/r/issues/comments?since={SINCE}" in calls[0][2]

    def test_a_failed_read_raises_instead_of_reporting_zero(self, mod, monkeypatch) -> None:
        def boom(argv, *, check=True):
            raise RuntimeError("gh failed")

        monkeypatch.setattr(mod, "run", boom)

        with pytest.raises(RuntimeError):
            mod.override_metrics("o/r", SINCE)


class TestTheRecordShapeMatchesTheHandler:
    def test_the_marker_pattern_is_the_contracts(self, mod) -> None:
        contract = load_skill_script("review_contract_for_metrics", CONTRACT)

        assert mod.OVERRIDE_MARKER_RE.pattern == contract.OVERRIDE_MARKER_RE.pattern

    def test_the_class_pattern_is_the_contracts(self, mod) -> None:
        contract = load_skill_script("review_contract_for_metrics", CONTRACT)
        if not hasattr(contract, "OVERRIDE_CLASS_RE"):
            pytest.skip("the class record is not on this base yet (D1b-1 adds OVERRIDE_CLASS_RE)")

        assert mod.OVERRIDE_CLASS_RE.pattern == contract.OVERRIDE_CLASS_RE.pattern

    def test_the_documented_example_is_read(self, mod) -> None:
        line = "<!-- ai-review-override-record class=deferred issue=1234 author_is_actor=false -->"
        body = f"{marker()}\n{line}"

        assert mod.OVERRIDE_CLASS_RE.match(body).groups() == ("deferred", "1234", "false")


class TestTheWeeklyReportCarriesThem:
    def _summary(self, mod, tmp_path: Path, monkeypatch, comments: list) -> tuple[str, dict]:
        def fake_run(argv, *, check=True):
            if argv[:2] == ["gh", "api"] and "issues/comments" in argv[2]:
                return json.dumps(comments)
            if argv[0] == "git" and "rev-list" in argv:
                return "0\n"
            if argv[0] == "git":
                return ""
            return "[]"

        monkeypatch.setattr(mod, "run", fake_run)
        monkeypatch.setattr(mod, "deferral_metrics", lambda repo: _DEFERRALS)
        # main fetches the merged-PR list once and hands it to the harvest.
        monkeypatch.setattr(mod, "merged_prs_since", lambda repo, since: [])
        monkeypatch.setattr(mod, "harvest_metrics", lambda repo, since, prs: _HARVEST)
        out, summary = tmp_path / "m.json", tmp_path / "m.md"
        monkeypatch.setattr(
            "sys.argv",
            [
                "fix_loop_metrics.py",
                "--since",
                "7 days ago",
                "--since-iso",
                SINCE,
                "--repo",
                "o/r",
                "--out",
                str(out),
                "--summary",
                str(summary),
            ],
        )
        assert mod.main() == 0
        return summary.read_text(encoding="utf-8"), json.loads(out.read_text(encoding="utf-8"))

    def test_the_json_and_the_table_carry_the_counters(self, mod, tmp_path, monkeypatch) -> None:
        text, data = self._summary(
            mod,
            tmp_path,
            monkeypatch,
            [classed("deferred", issue="9", author="true"), classed("owner-decision", pr=12)],
        )

        assert data["overrides"]["overrides_total"] == 2
        assert "| Overrides recorded / by the PR author on their own PR | 2 / 1 |" in text
        assert "| Overrides by class | deferred 1, owner-decision 1 |" in text
        assert "| Deferred overrides with no issue | 0 |" in text
        assert "| Owner decisions | 1: #12 by @writer |" in text

    def test_an_empty_week_prints_none_not_a_blank(self, mod, tmp_path, monkeypatch) -> None:
        text, _data = self._summary(mod, tmp_path, monkeypatch, [])

        assert "| Overrides by class | none |" in text
        assert "| Owner decisions | 0: none |" in text

    def test_a_long_owner_decision_list_is_cut_and_says_so(
        self, mod, tmp_path, monkeypatch
    ) -> None:
        many = [classed("owner-decision", pr=n) for n in range(1, mod.OWNER_DECISION_LISTED + 4)]
        text, data = self._summary(mod, tmp_path, monkeypatch, many)

        assert len(data["overrides"]["owner_decisions"]) == len(many)
        assert "; and 3 more |" in text


class TestTheJobCanReadTheComments:
    def test_the_token_can_read_issue_comments(self) -> None:
        # An explicit `permissions:` block grants nothing it does not list, and the
        # script now reads repo-wide issue comments. A missing grant is a 403 that
        # costs the whole weekly report.
        script = METRICS_SCRIPT.read_text(encoding="utf-8")
        assert "issues/comments" in script
        doc = yaml.safe_load(ANALYSIS.read_text(encoding="utf-8"))
        blocks = {"the workflow": doc.get("permissions") or {}}
        for job_id, job in (doc.get("jobs") or {}).items():
            if isinstance(job, dict) and "permissions" in job:
                blocks[f"job `{job_id}`"] = job["permissions"] or {}
        for where, granted in blocks.items():
            assert granted.get("issues") in ("read", "write"), where

    def test_the_narration_prompt_names_the_override_numbers(self) -> None:
        text = ANALYSIS.read_text(encoding="utf-8")

        assert "`overrides` in metrics.json" in text
        assert "`owner-decision`" in text


_DEFERRALS = {"open": 0, "tracked": 0, "tracked_pct": None, "overdue": 0}
_HARVEST = {
    "merged_fix_prs": 0,
    "with_harvest_section": 0,
    "adoption_pct": None,
    "escapes": 0,
    "excluded_other_base": 0,
    "excluded_gate_never_ran": 0,
}
