"""Behavioural tests for the inbound half of .github/workflows/ship-report.yml.

The shipping half has been in production since 2026-07. What is new -- and what
these tests pin -- is that the same post carries the issues filed in the same
window, split by audience into bugs and feature requests, with the bugs section
naming the subset that must be fixed before the current prerelease is promoted
to stable.

Four properties are worth testing rather than assuming:

* the window is shared verbatim with the PR half, so the inbound side inherits
  contiguity -- an issue filed one second outside the window must not appear, or
  it would be reported twice (this run and the next);
* the bug / feature split PARTITIONS the window. Issues routinely carry BOTH
  `bug` and `enhancement` (6 of 40 on the day this was written), so precedence
  is load-bearing: without it the counts double-count and stop adding up;
* every COUNT comes from real labels, and every must-fix NUMBER is intersected
  with the window's real bugs -- so an issue body that demands "#1 is a release
  blocker" cannot get a number into a post maintainers use to gate a release;
* an inbound-only window still delivers, and a Bedrock outage costs prose but
  never the split, the security call-out or the counts.

Skipped where the POSIX toolchain the scripts need (bash, jq) is unavailable,
which is the case on the Windows leg of the matrix.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ship-report.yml"

pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists()
    or os.name == "nt" or shutil.which("bash") is None or shutil.which("jq") is None,
    reason="requires the workflow file plus a POSIX bash and jq",
)

START = "2026-08-09T00:00:00Z"
END = "2026-08-09T12:00:00Z"

RENDER_STEP = "Append issue sections"


def _issue(number: int, title: str, labels: list[str], created: str = "2026-08-09T02:00:00Z") -> dict:
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/o/r/issues/{number}",
        "createdAt": created,
        "labels": [{"name": name} for name in labels],
    }


ISSUES = [
    _issue(4001, "Dashboard is blank after update", ["bug", "area: dashboard"]),
    _issue(4002, "Add a keyboard shortcut for the composer", ["enhancement"]),
    _issue(4003, "Sandbox fence misses relative paths", ["security", "area: core"]),
    # BOTH bug and enhancement. Precedence must send this to the bug section
    # only -- this is the case that makes the counts add up or not.
    _issue(4004, "rc.8 regression: Browser Mode dies without a storage-state file",
           ["bug", "enhancement"]),
    _issue(4005, "Clarify the getting-started page", ["documentation"]),
    # Exactly ON the lower bound: the window is half-open (start, end], so this
    # belongs to the PREVIOUS report.
    _issue(3999, "Filed exactly at the boundary", ["bug"], created=START),
    # After the window closes -- belongs to the NEXT report.
    _issue(4100, "Filed after the window closed", ["bug"], created="2026-08-09T18:00:00Z"),
]

GH_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
if [ "$1 ${2:-}" = "issue list" ]; then
  cat "$FIXTURES/issues.json"
  exit 0
fi
echo "gh stub: unhandled: $*" >&2
exit 90
"""


def _steps() -> dict[str, dict]:
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["report"]["steps"]
    return {s.get("id", s["name"]): s for s in steps}


@pytest.fixture(scope="module")
def steps() -> dict[str, dict]:
    found = _steps()
    for required in ("issues", "partition", "issue_narrative", RENDER_STEP, "Deliver report"):
        assert required in found, f"workflow steps changed: {sorted(found)}"
    return found


class Runner:
    def __init__(self, root: Path, steps: dict[str, dict]) -> None:
        self.steps = steps
        self.fixtures = root / "fixtures"
        self.work = root / "work"
        bindir = root / "bin"
        for d in (self.fixtures, self.work, bindir):
            d.mkdir(parents=True)
        stub = bindir / "gh"
        stub.write_text(GH_STUB)
        stub.chmod(0o755)
        self.outputs_file = self.work / "gh_output"
        self.outputs_file.touch()
        self.env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(self.fixtures),
            "GH_TOKEN": "stub",
            "GITHUB_REPOSITORY": "kirodotdev/KiroCrew",
            "GITHUB_OUTPUT": str(self.outputs_file),
            "START": START,
            "END": END,
        }

    def _run(self, name: str, script: str | None = None, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv, test-local stubs
            ["bash", "-c", script if script is not None else self.steps[name]["run"]],
            cwd=self.work,
            env={**self.env, **extra},
            text=True,
            capture_output=True,
        )

    def _outputs(self) -> dict[str, str]:
        return dict(
            line.split("=", 1) for line in self.outputs_file.read_text().splitlines() if "=" in line
        )

    def fetch(self, issues: list[dict] | None = None) -> dict[str, str]:
        """Run the issues fetch + partition; return the partition counts."""
        (self.fixtures / "issues.json").write_text(json.dumps(ISSUES if issues is None else issues))
        self.outputs_file.write_text("")
        step = self._run("issues")
        assert step.returncode == 0, step.stderr
        self.issues_stdout = step.stdout
        self.issue_count = self._outputs()["count"]
        if self.issue_count == "0":
            return {}
        self.outputs_file.write_text("")
        step = self._run("partition")
        assert step.returncode == 0, step.stderr
        self.part = self._outputs()
        return self.part

    def render(self, verdict: dict | None = None) -> str:
        """Run the two-section render, substituting ${{ }} the way Actions does."""
        target = self.work / "issue-verdict.json"
        target.unlink(missing_ok=True)
        if verdict is not None:
            target.write_text(json.dumps(verdict))

        script = self.steps[RENDER_STEP]["run"]
        for key, value in self.part.items():
            script = script.replace("${{ steps.partition.outputs.%s }}" % key, value)
        assert not re.search(r"\$\{\{", script), "unsubstituted expression left in the script"

        step = self._run(RENDER_STEP, script=script)
        assert step.returncode == 0, step.stderr
        self.render_stdout = step.stdout
        return (self.work / "body.txt").read_text()

    @property
    def selected(self) -> list[int]:
        return [i["number"] for i in json.loads((self.work / "issues.json").read_text())]

    @property
    def split(self) -> dict[str, list[dict]]:
        return json.loads((self.work / "split.json").read_text())


@pytest.fixture()
def runner(tmp_path: Path, steps: dict[str, dict]) -> Runner:
    return Runner(tmp_path, steps)


VERDICT = {
    "must_fix": [{"number": 4004, "why": "rc.8 regression, Browser Mode dies without the file"}],
    "bugs": "Defect intake is dashboard and core internals.",
    "features": "One request, for a composer shortcut (#4002).",
}


# ── The shared window ────────────────────────────────────────────────────────


def test_only_issues_inside_the_window_are_reported(runner: Runner) -> None:
    """The half-open window is what makes the digest at-least-once, not -twice."""
    runner.fetch()
    assert runner.selected == [4001, 4002, 4003, 4004, 4005]


def test_boundary_issue_belongs_to_the_previous_report(runner: Runner) -> None:
    runner.fetch()
    assert 3999 not in runner.selected and 4100 not in runner.selected


def test_an_inbound_spike_does_not_fail_the_run(runner: Runner) -> None:
    """Unlike the 300-PR cap, overflow here must NOT withhold the report.

    Failing would leave the window unadvanced, so a quiet inbound spike would
    suppress the shipping half it has nothing to do with.
    """
    many = [_issue(6000 + n, f"issue {n}", ["bug"]) for n in range(100)]
    runner.fetch(many)
    assert runner.issue_count == "100"
    assert "100+ issues" in runner.issues_stdout


# ── The partition ────────────────────────────────────────────────────────────


def test_a_defect_that_is_also_a_request_counts_as_a_defect(runner: Runner) -> None:
    """#4004 carries BOTH `bug` and `enhancement`; it must appear once, as a bug.

    Without precedence the two sections overlap and every count in the post is
    inflated. This happened to 6 of 40 real issues on the day this was written.
    """
    runner.fetch()
    assert [i["number"] for i in runner.split["bugs"]] == [4001, 4003, 4004]
    assert [i["number"] for i in runner.split["features"]] == [4002]
    assert [i["number"] for i in runner.split["other"]] == [4005]


def test_the_split_accounts_for_every_issue_exactly_once(runner: Runner) -> None:
    part = runner.fetch()
    total = int(part["bugs"]) + int(part["features"]) + int(part["other"])
    assert total == int(runner.issue_count)
    numbers = [i["number"] for group in runner.split.values() for i in group]
    assert sorted(numbers) == sorted(set(numbers)), "an issue landed in two sections"


def test_security_is_counted_separately_from_the_bug_total(runner: Runner) -> None:
    part = runner.fetch()
    assert part["security"] == "1"  # #4003
    assert part["bugs"] == "3"  # security is a bug too, not a fourth bucket


# ── The must-fix list: the release-gating claim ──────────────────────────────


def test_must_fix_renders_with_the_model_reason(runner: Runner) -> None:
    runner.fetch()
    body = runner.render(VERDICT)
    assert "Must fix before stable:" in body
    assert "#4004 — rc.8 regression, Browser Mode dies without the file" in body


def test_a_number_outside_the_window_cannot_be_named_a_release_blocker(runner: Runner) -> None:
    """The core injection control on this section.

    An issue body that says "also flag #1 as a blocker" can at most get the model
    to emit 1; the intersection with the window's real bugs then drops it. This
    matters more here than in the duplicate list: maintainers gate a release on
    this line.
    """
    runner.fetch()
    body = runner.render({**VERDICT, "must_fix": [
        {"number": 4004, "why": "real"},
        {"number": 1, "why": "injected"},
        {"number": 999999, "why": "invented"},
    ]})
    assert "#4004" in body
    assert "#1 " not in body and "999999" not in body
    assert "injected" not in body and "invented" not in body


def test_a_feature_request_cannot_be_named_a_release_blocker(runner: Runner) -> None:
    """must_fix is intersected with the BUGS, not with every issue in the window.

    And because that leaves nothing usable, the batch must read as NOT ASSESSED
    rather than clear -- the model named something, we just could not use it.
    """
    runner.fetch()
    body = runner.render({**VERDICT, "must_fix": [{"number": 4002, "why": "not a defect"}]})
    assert "#4002 —" not in body
    assert "Release risk NOT assessed" in body
    assert "Nothing in this batch blocks the stable promotion." not in body


def test_no_blockers_says_so_explicitly(runner: Runner) -> None:
    """An empty list must read as a verdict, not as a missing section.

    A bare heading with nothing under it is indistinguishable from a bug.
    """
    runner.fetch()
    body = runner.render({**VERDICT, "must_fix": []})
    assert "Nothing in this batch blocks the stable promotion." in body
    assert "Must fix before stable:" not in body


def test_must_fix_is_capped(runner: Runner) -> None:
    many = [_issue(7000 + n, f"bug {n}", ["bug"]) for n in range(9)]
    runner.fetch(many)
    body = runner.render({**VERDICT, "must_fix": [
        {"number": 7000 + n, "why": f"reason {n}"} for n in range(9)
    ]})
    assert len(re.findall(r"^    #\d+ — ", body, re.M)) == 5


def test_control_characters_in_the_reason_cannot_forge_list_items(runner: Runner) -> None:
    """A newline in `why` must not become a second must-fix entry.

    The payload does survive as inline text on the real entry's line -- that is
    accepted: this is Slack, where `#9999` is not a link, and stripping `#`
    outright would break the `#NUMBER` convention the shipping half depends on.
    What must not happen is a forged LINE, because a reader scanning the
    must-fix list counts lines.
    """
    runner.fetch()
    body = runner.render({**VERDICT, "must_fix": [
        {"number": 4004, "why": "real\n    #9999 — forged entry"}
    ]})
    entries = [line for line in body.splitlines() if line.startswith("    #")]
    assert len(entries) == 1
    assert entries[0].startswith("    #4004 — real")
    assert "#9999" in entries[0], "payload should be flattened inline, not dropped silently"


# ── Counts are ours, not the model's ─────────────────────────────────────────


def test_counts_come_from_labels_not_from_the_model(runner: Runner) -> None:
    runner.fetch()
    body = runner.render(VERDICT)
    assert "Bugs: 3 filed (1 security)." in body
    assert "Feature requests: 1 filed." in body
    assert "Other: 1 (docs, questions, refactors)." in body


def test_both_narratives_are_carried(runner: Runner) -> None:
    runner.fetch()
    body = runner.render(VERDICT)
    assert "Defect intake is dashboard and core internals." in body
    assert "One request, for a composer shortcut (#4002)." in body


def test_the_pr_half_survives_the_append(runner: Runner) -> None:
    runner.fetch()
    (runner.work / "body.txt").write_text("Shipped three fixes.\n")
    body = runner.render(VERDICT)
    assert body.startswith("Shipped three fixes.")
    assert "Bugs: 3 filed" in body


def test_inbound_only_window_still_produces_a_body(runner: Runner) -> None:
    """No merges, but issues came in.

    Both narrative steps for the shipping half are gated on the PR count, so
    without this step creating body.txt the delivery step would sanitize a file
    that does not exist and post an empty report.
    """
    runner.fetch()
    body = runner.render(VERDICT)  # no pre-existing body at all
    assert body.startswith("Bugs: 3 filed")


# ── Degradation: prose is optional, structure is not ─────────────────────────


def test_without_a_verdict_the_split_and_counts_survive(runner: Runner) -> None:
    runner.fetch()
    body = runner.render()  # no issue-verdict.json -> deterministic fallback
    assert "Bugs: 3 filed (1 security)." in body
    assert "Feature requests: 1 filed." in body
    assert "no issue narrative" in runner.render_stdout


def test_the_fallback_calls_out_security_as_release_risk(runner: Runner) -> None:
    runner.fetch()
    body = runner.render()
    assert "Security — treat as release risk:" in body
    assert "#4003 Sandbox fence misses relative paths" in body


def test_the_fallback_lists_each_bug_once(runner: Runner) -> None:
    """Regression: the first draft listed security issues in BOTH sub-lists.

    That wasted a re-read and made the "+N more" remainder a lie.
    """
    runner.fetch()
    body = runner.render()
    assert body.count("#4003") == 1
    assert body.count("#4001") == 1


def test_the_fallback_remainder_count_excludes_what_it_showed(runner: Runner) -> None:
    many = (
        [_issue(8000, "sec", ["security"])]
        + [_issue(8100 + n, f"bug {n}", ["bug"]) for n in range(10)]
    )
    runner.fetch(many)
    body = runner.render()
    # 10 non-security defects, 4 shown -> 6 remain. The security one is in its
    # own sub-list and must not be counted here.
    assert "+6 more" in body


# ── Malformed model output must degrade, not crash ───────────────────────────


@pytest.mark.parametrize("bad", ["none", 42, {"n": 1}, True])
def test_a_non_array_must_fix_falls_back_instead_of_clearing_the_release(
    runner: Runner, bad: object
) -> None:
    """Unusable model output must never become an affirmative all-clear.

    `// []` only catches null, so `"none"[]` used to be a jq ERROR (exit 5) that
    failed the step -- and because a failed run does not advance the window, the
    digest would be withheld AND repeat. But coercing it to an empty array is
    WORSE: the report would then print "Nothing in this batch blocks the stable
    promotion", turning garbage into a release clearance maintainers act on.
    The only safe answer is the deterministic list, which claims nothing.
    """
    runner.fetch()
    body = runner.render({**VERDICT, "must_fix": bad})
    assert "Nothing in this batch blocks the stable promotion." not in body
    assert "Security — treat as release risk:" in body  # the fallback rendered
    assert "Bugs: 3 filed (1 security)." in body


def test_all_entries_dropped_is_reported_as_not_assessed(runner: Runner) -> None:
    """A named-but-unusable list is not the same as an empty one.

    If the model named only numbers we had to drop (junk entries, or issues
    outside this window), it did not tell us the batch is clear -- it told us
    nothing we could use. Saying "nothing blocks" there is the same false
    clearance by a different route.
    """
    runner.fetch()
    body = runner.render({**VERDICT, "must_fix": [
        {"number": 1, "why": "outside the window"},
        {"number": 999999, "why": "invented"},
    ]})
    assert "Release risk NOT assessed" in body
    assert "Nothing in this batch blocks the stable promotion." not in body
    # And it must hand the reader the deterministic security list instead of
    # leaving them with no signal at all.
    assert "#4003 Sandbox fence misses relative paths" in body


def test_an_empty_list_is_still_a_genuine_all_clear(runner: Runner) -> None:
    """The distinction has to cut both ways, or it is just noise.

    `must_fix: []` means the model evaluated the batch and found nothing --
    that IS the all-clear, and must keep reading as one.
    """
    runner.fetch()
    body = runner.render({**VERDICT, "must_fix": []})
    assert "Nothing in this batch blocks the stable promotion." in body
    assert "Release risk NOT assessed" not in body


# ── Untrusted text must not publish a clickable link ─────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        "see https://evil.example/login",
        "see HTTPS://EVIL.EXAMPLE/login",  # jq gsub is case-sensitive by default
        "see www.evil.example/login",  # Slack autolinks a bare www. host too
    ],
)
def test_model_prose_cannot_publish_a_link(runner: Runner, payload: str) -> None:
    runner.fetch()
    body = runner.render({
        "must_fix": [{"number": 4004, "why": payload}],
        "bugs": payload,
        "features": payload,
    })
    assert "evil.example" not in body
    assert "(link removed)" in body


def test_the_fallback_strips_links_from_issue_titles(runner: Runner) -> None:
    """A bug-form title is attacker-writable, and the delivery sanitizer
    downstream only neutralizes angle brackets."""
    hostile = [_issue(4001, "crash, details at https://evil.example/x", ["bug"])]
    runner.fetch(hostile)
    body = runner.render()  # fallback path
    assert "evil.example" not in body
    assert "(link removed)" in body


# ── The credentials gate must serve BOTH narratives ──────────────────────────


def test_credentials_are_configured_for_an_inbound_only_window() -> None:
    """Gating creds on PRs alone silently disables the issue narrative.

    On a window with issues but no merges -- exactly where the issue half is the
    entire post -- a PR-only gate skips credentials, so `issue_narrative` never
    runs and the promised summary always degrades to the fallback list.
    """
    condition = _steps()["creds"]["if"]
    assert "steps.issues.outputs.count != '0'" in condition
    assert "||" in condition


# ── Static contract ──────────────────────────────────────────────────────────


def test_delivery_fires_when_only_issues_are_present() -> None:
    """PR-only would drop an inbound-only window."""
    condition = _steps()["Deliver report"]["if"]
    assert "steps.issues.outputs.count != '0'" in condition
    assert "steps.fetch.outputs.count != '0'" in condition
    assert "||" in condition


def test_workflow_declares_the_issue_read_permission() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    # issues:read and nothing more -- this lane must never write to an issue;
    # the commenting lane is issue-summary.yml.
    assert workflow["permissions"]["issues"] == "read"


def test_issue_text_reaches_the_same_sanitizer_as_pr_titles() -> None:
    """Issue titles and model prose are untrusted; the sections must be appended
    BEFORE delivery, which is where the Slack escaping happens."""
    order = [
        s.get("id", s["name"])
        for s in yaml.safe_load(WORKFLOW.read_text())["jobs"]["report"]["steps"]
    ]
    assert order.index(RENDER_STEP) < order.index("Deliver report")


def test_the_two_narratives_are_independent_model_calls() -> None:
    """One model failure must not silence the other half.

    The shipping narrative and the issue narrative are separate steps, each
    `continue-on-error`, each with its own deterministic fallback.
    """
    steps = _steps()
    assert steps["bedrock"]["continue-on-error"] is True
    assert steps["issue_narrative"]["continue-on-error"] is True
    assert steps["issue_narrative"]["id"] != steps["bedrock"]["id"]
