"""Behavioural tests for pr-readiness.yml's server-side disposition gate.

Issue #6658: the one-lane / one-rationale-per-finding disposition rule was
mechanical only for a writer running the prepare-pr loop. A writer who skipped
that loop could post a blanket single-rationale ``target=gpt`` record, which
codex-review.yml's adjudication ledger admits with full downgrade power, while
nothing on the merge path objected. Readiness publishes the repository's sole
required status, so evaluating the rule there is what binds every writer.

These tests extract the real "Evaluate disposition records" step and execute it
with ``gh`` replaced by a stub, against the REAL ``pr_status.py``
``--disposition-gate`` mode -- the point being that the rule is evaluated by the
one parity-pinned implementation, not by a workflow-side copy of the grammar. A
companion class pins the wiring, because a step whose outputs nothing reads is a
gate that silently enforces nothing.

Skipped where the POSIX toolchain the step needs (bash, jq) is unavailable,
mirroring test_pr_readiness_evaluate.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "pr-readiness.yml"
GATE = (
    ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "prepare-pr"
    / "scripts"
    / "pr_status.py"
)

pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists()
    or not GATE.exists()
    or os.name == "nt"
    or shutil.which("bash") is None
    or shutil.which("jq") is None,
    reason="requires the workflow plus a POSIX bash and jq",
)

HEAD = "f" * 40

# ``gh`` stub serving the two endpoints the gate reads: the PR's issue comments
# and one collaborator-permission lookup per distinct disposition author.
GH_STUB = r"""#!/usr/bin/env bash
set -uo pipefail
url=""
for arg in "$@"; do
  case "$arg" in repos/*) url="$arg" ;; esac
done
case "$url" in
  *"/collaborators/"*"/permission"*)
    login="$(printf '%s' "$url" | awk -F/ '{print $5}')"
    if [ -f "$FIXTURES/permissions.json" ]; then
      jq -er --arg l "$login" '.[$l] // empty' "$FIXTURES/permissions.json" \
        | jq -R '{permission: .}'
      exit 0
    fi
    echo "gh: Not Found (HTTP 404)" >&2
    exit 1 ;;
  *"/issues/"*"/comments"*)
    if [ -f "$FIXTURES/comments_fail" ]; then
      echo "gh: Server Error (HTTP 500)" >&2
      exit 1
    fi
    cat "$FIXTURES/comments.json"
    exit 0 ;;
esac
echo "gh stub: unhandled: $*" >&2
exit 90
"""


def _steps() -> list[dict]:
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return spec["jobs"]["readiness"]["steps"]


def _step(step_id: str) -> dict:
    for step in _steps():
        if step.get("id") == step_id:
            return step
    raise AssertionError("step not found: {}".format(step_id))


def _bot_comment(head: str = HEAD) -> dict:
    """A GPT-lane review comment carrying one finding stamped for ``head``."""
    return {
        "id": 1,
        "user": {"type": "Bot", "login": "github-actions[bot]"},
        "body": (
            "<!-- codex-ai-review -->\n"
            "FINDING -- src/x.py:10 -- tighten the guard -> Fix: widen it\n"
            "[GPT-REVIEWED] " + head
        ),
    }


def _disposition(body: str, login: str = "alice", comment_id: int = 900) -> dict:
    return {"id": comment_id, "user": {"type": "User", "login": login}, "body": body}


class GateRunner:
    """Executes the disposition-gate step against one stubbed comment set."""

    def __init__(self, root: Path) -> None:
        self.fixtures = root / "fixtures"
        bindir = root / "bin"
        self.temp = root / "runner_temp"
        for d in (self.fixtures, bindir, self.temp):
            d.mkdir(parents=True)
        stub = bindir / "gh"
        stub.write_text(GH_STUB)
        stub.chmod(0o755)
        self.output = root / "github_output"
        self.output.touch()
        self.env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(self.fixtures),
            "RUNNER_TEMP": str(self.temp),
            "GITHUB_OUTPUT": str(self.output),
            "GH_TOKEN": "stub",
            "REPO": "kirodotdev/KiroCrew",
            "PR": "6658",
            "SHA": HEAD,
            "GATE": str(GATE),
        }
        (self.fixtures / "comments.json").write_text("[]")
        (self.fixtures / "permissions.json").write_text(json.dumps({"alice": "write"}))

    def run(self, *, comments: list[dict] | None = None, gate: str | None = None):
        if comments is not None:
            (self.fixtures / "comments.json").write_text(json.dumps(comments))
        env = dict(self.env)
        if gate is not None:
            env["GATE"] = gate
        self.output.write_text("")
        proc = subprocess.run(
            ["bash", "-c", _step("dispositions")["run"]],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=self.temp,
        )
        return proc, self._outputs()

    def fail_comment_reads(self) -> None:
        (self.fixtures / "comments_fail").touch()

    def drop_permission_api(self) -> None:
        (self.fixtures / "permissions.json").unlink()

    def _outputs(self) -> dict[str, str]:
        """Parse $GITHUB_OUTPUT, including the multi-line heredoc form."""
        outputs: dict[str, str] = {}
        lines = self.output.read_text().splitlines()
        i = 0
        while i < len(lines):
            key, _, value = lines[i].partition("=")
            if value.startswith("<<"):
                delim = value[2:]
                body: list[str] = []
                i += 1
                while i < len(lines) and lines[i] != delim:
                    body.append(lines[i])
                    i += 1
                outputs[key] = "\n".join(body).strip("\n")
            else:
                key, _, value = lines[i].partition("<<")
                if value:
                    delim = value
                    body = []
                    i += 1
                    while i < len(lines) and lines[i] != delim:
                        body.append(lines[i])
                        i += 1
                    outputs[key] = "\n".join(body).strip("\n")
                else:
                    key, _, value = lines[i].partition("=")
                    outputs[key] = value
            i += 1
        return outputs


@pytest.fixture()
def gate(tmp_path: Path) -> GateRunner:
    return GateRunner(tmp_path)


class TestTheStepEvaluatesTheRealRule:
    def test_a_blanket_record_is_reported_as_a_violation(self, gate: GateRunner):
        """The exact gap #6658 names: a writer-authored record naming the GPT
        lane but claiming no span, while that lane has a live finding."""
        proc, outputs = gate.run(
            comments=[
                _bot_comment(),
                _disposition(
                    "<!-- ai-review-disposition target=gpt head=" + HEAD + " -->\n"
                    "all three findings are false positives"
                ),
            ]
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["ok"] == "true"
        assert "claims no span= finding identity" in outputs["violations"]
        assert "comment 900 by alice" in outputs["violations"]

    def test_a_conforming_record_produces_no_violation(self, gate: GateRunner):
        span = _span_of("src/x.py", "gpt/FINDING")
        proc, outputs = gate.run(
            comments=[
                _bot_comment(),
                _disposition(
                    "<!-- ai-review-disposition target=gpt head=" + HEAD + " -->\n"
                    f"- **rebutted** span={span}\n"
                    "> the guard is unreachable on this path"
                ),
            ]
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["ok"] == "true"
        assert outputs["violations"] == ""

    def test_a_cross_lane_claim_is_reported(self, gate: GateRunner):
        """One comment covers exactly one lane: a record targeting opus cannot
        rule on a GPT finding."""
        span = _span_of("src/x.py", "gpt/FINDING")
        proc, outputs = gate.run(
            comments=[
                _bot_comment(),
                _disposition(
                    "<!-- ai-review-disposition target=opus head=" + HEAD + " -->\n"
                    f"- **rebutted** span={span}"
                ),
            ]
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["ok"] == "true"
        assert "cross-lane disposition" in outputs["violations"]

    def test_each_violation_is_one_self_contained_line(self, gate: GateRunner):
        """The evaluation step reads one violation per line, so a record tripping
        several classes must produce several COMPLETE lines -- no violation
        wrapped across two, which would forge a blocker out of a fragment."""
        proc, outputs = gate.run(
            comments=[
                _bot_comment(),
                _disposition(
                    "<!-- ai-review-disposition target=gpt head=" + HEAD + " -->\n"
                    "- **rebutted** span=aaaaaaaaaaaa\n"
                    "- **accepted** span=bbbbbbbbbbbb"
                ),
            ]
        )
        assert proc.returncode == 0, proc.stderr
        lines = outputs["violations"].splitlines()
        # This record trips multi-span, multi-bullet and two unresolvable
        # claims; the count is the evaluator's business, the SHAPE is ours.
        assert len(lines) > 1
        for line in lines:
            assert line.strip() == line and line
            # Every violation names the record it rules on, so a fragment
            # (a wrapped continuation) would be missing this.
            assert "comment 900 by alice" in line

    def test_a_non_writer_record_cannot_block(self, gate: GateRunner):
        """Enforcement scope equals the adjudication ledger's admission scope:
        an author the permission API does not confirm as a writer is dropped
        here exactly as codex-review.yml drops them, so the gate never blocks on
        a record that holds no downgrade power."""
        (gate.fixtures / "permissions.json").write_text(json.dumps({"mallory": "read"}))
        proc, outputs = gate.run(
            comments=[
                _bot_comment(),
                _disposition(
                    "<!-- ai-review-disposition target=gpt head=" + HEAD + " -->\nno span here",
                    login="mallory",
                ),
            ]
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["ok"] == "true"
        assert outputs["violations"] == ""

    def test_an_unavailable_permission_api_degrades_to_no_enforcement(self, gate: GateRunner):
        """Same direction when the permission lookup itself fails: unverifiable
        means ignored, never blocked -- and the ledger cannot admit the record
        either, since it makes the identical call with the identical token."""
        gate.drop_permission_api()
        proc, outputs = gate.run(
            comments=[
                _bot_comment(),
                _disposition(
                    "<!-- ai-review-disposition target=gpt head=" + HEAD + " -->\nno span here"
                ),
            ]
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["ok"] == "true"
        assert outputs["violations"] == ""


class TestTheStepNeverFailsTheJob:
    """A failed step skips the publish step below it, which leaves the required
    status pending with no further event able to recompute it on an unchanged
    commit. Every trouble path must therefore exit 0 and say ok=false."""

    def test_unreadable_comments_report_not_ok_without_failing(self, gate: GateRunner):
        gate.fail_comment_reads()
        proc, outputs = gate.run(comments=[_bot_comment()])
        assert proc.returncode == 0, proc.stderr
        assert outputs["ok"] == "false"
        assert outputs["violations"] == ""

    def test_a_missing_evaluator_reports_not_ok_without_failing(self, gate: GateRunner):
        proc, outputs = gate.run(gate=str(GATE.parent / "does_not_exist.py"))
        assert proc.returncode == 0, proc.stderr
        assert outputs["ok"] == "false"
        assert "not found" in proc.stderr

    def test_a_crashing_evaluator_reports_not_ok_without_failing(
        self, gate: GateRunner, tmp_path: Path
    ):
        broken = tmp_path / "broken.py"
        broken.write_text("import sys\nsys.exit(3)\n")
        proc, outputs = gate.run(gate=str(broken))
        assert proc.returncode == 0, proc.stderr
        assert outputs["ok"] == "false"

    def test_an_empty_comment_set_is_ok_and_silent(self, gate: GateRunner):
        proc, outputs = gate.run(comments=[])
        assert proc.returncode == 0, proc.stderr
        assert outputs["ok"] == "true"
        assert outputs["violations"] == ""


class TestTheGateIsWiredIntoTheVerdict:
    """A gate whose output nothing reads enforces nothing. These pin the three
    joints a rename or a re-order could quietly break."""

    def test_the_verdict_step_consumes_the_gate_outputs(self):
        env = _step("verdict")["env"]
        assert env["DISPOSITION_OK"] == "${{ steps.dispositions.outputs.ok }}"
        assert env["DISPOSITION_VIOLATIONS"] == "${{ steps.dispositions.outputs.violations }}"

    def test_the_gate_runs_before_the_verdict(self):
        ids = [step.get("id") for step in _steps()]
        assert ids.index("dispositions") < ids.index("verdict")

    def test_the_gate_and_the_verdict_share_one_run_condition(self):
        """A gate that skips while the verdict publishes would report a clean
        rule from a check that never ran."""
        assert _step("dispositions")["if"] == _step("verdict")["if"]

    def test_the_verdict_treats_a_violation_as_blocking_and_unknown_as_waiting(self):
        script = _step("verdict")["run"]
        assert 'failed+=("disposition rule: $violation")' in script
        assert 'pending+=("disposition records could not be read")' in script

    def test_the_evaluator_is_checked_out_from_a_trusted_ref(self):
        """This workflow is pull_request_target and holds write tokens, so the
        bytes it executes must never come from the PR head."""
        checkout = next(step for step in _steps() if "actions/checkout" in (step.get("uses") or ""))
        ref = checkout["with"]["ref"]
        assert "default_branch" in ref
        assert "pull_request" not in ref and "head" not in ref
        assert checkout["with"]["persist-credentials"] is False

    def test_the_gate_invokes_the_parity_pinned_evaluator(self):
        """The rule must not gain a workflow-side fourth copy of the grammar:
        the step calls pr_status.py's mode, and nothing else parses the marker."""
        step = _step("dispositions")
        assert step["env"]["GATE"].endswith("prepare-pr/scripts/pr_status.py")
        assert "--disposition-gate" in step["run"]
        assert "ai-review-disposition" not in step["run"]


def _span_of(path: str, rule_class: str) -> str:
    """span_hash without importing the script: sha256(path|rule_class)[:12]."""
    import hashlib

    return hashlib.sha256("{}|{}".format(path, rule_class).encode("utf-8")).hexdigest()[:12]
