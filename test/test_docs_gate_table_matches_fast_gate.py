from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FAST_GATE = ROOT / ".github" / "workflows" / "fast-gate.yml"
GATE_DOC = ROOT / "docs" / "ci" / "ci-and-reviews.md"

_JOB_RE = re.compile(r"^  ([a-z0-9][a-z0-9-]*):\s*$")
_DOC_ROW_RE = re.compile(r"^\|\s*`([a-z0-9][a-z0-9-]*)`")


def fast_gate_jobs() -> set[str]:
    text = FAST_GATE.read_text(encoding="utf-8").splitlines()
    jobs: set[str] = set()
    in_jobs = False
    for line in text:
        if not in_jobs:
            if line == "jobs:":
                in_jobs = True
            continue
        match = _JOB_RE.match(line)
        if match:
            jobs.add(match.group(1))
    return jobs


def documented_gate_jobs() -> set[str]:
    return _documented_rows("| Job |")


def documented_gate_steps() -> set[str]:
    return _documented_rows("| Gate step |")


def _documented_rows(header_prefix: str) -> set[str]:
    text = GATE_DOC.read_text(encoding="utf-8").splitlines()
    rows: set[str] = set()
    in_section = False
    in_table = False
    for line in text:
        if line.startswith("## `fast-gate.yml`"):
            in_section = True
            continue
        if in_section and not in_table:
            if line.startswith(header_prefix):
                in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            match = _DOC_ROW_RE.match(line)
            if match and match.group(1) not in ("Job", "Gate"):
                rows.add(match.group(1))
    return rows


# The doc row each gate step is written up under, keyed by the script the step
# runs. A display mapping only: which gates EXIST is read out of the workflow by
# fast_gate_source_gate_scripts(), so a gate missing from this dict is reported
# as undocumented rather than dropped from both sides of the parity assertion.
_STEP_DOC_NAMES = {
    "scripts/verify_vendor_manifest.py": "vendor-manifest",
    "scripts/check_brand_name.py": "brand-lint",
    "scripts/check_comment_history.py": "comment-history-lint",
    "scripts/check_focus_cue.py": "focus-cue-lint",
    "scripts/check_feature_map.py": "feature-map-lint",
    "scripts/check_changelog_history.py": "changelog-history",
    "scripts/check_decisions_history.py": "decision-ledger-history",
    "scripts/check_builtin_skill_scope.py": "builtin-skill-scope",
    "scripts/check_loop_bound_locks.py": "loop-bound-locks",
    "scripts/check_testpaths_coverage.py": "testpaths-coverage",
    "scripts/check_cwd_relative_repo_reads.py": "cwd-relative-repo-reads",
    "scripts/check_harness_parity.py": "harness-parity",
    "scripts/check_memory_store_seam.py": "memory-store-seam",
    "scripts/docs-lint.sh": "docs-lint",
}


#: The separators that begin another command on one line. A gate is routinely
#: invoked as `<script> --test && <script>`, so the self-test rule below has to
#: judge each command and not the whole line, or that gate reads as a self-test.
_COMMAND_SPLIT_RE = re.compile(r"&&|\|\||[;|]")
#: A repository gate script as the workflow invokes it: `scripts/<name>.py` or
#: `.sh`, reached bare, through `./`, or through a workspace-absolute path.
#: `.github/scripts/` holds the CI helpers the steps call, not gates.
_SCRIPT_TOKEN_RE = re.compile(r"(?:^|/)(scripts/[A-Za-z0-9_.-]+\.(?:py|sh))$")
_HELPER_DIR = ".github/scripts/"


def fast_gate_source_gate_scripts() -> set[str]:
    """Every gate script the `source-gates` job enforces, read from the workflow.

    Taken from the file and not from `_STEP_DOC_NAMES`, so a gate added as a new
    step of that job appears here even when nothing documents it, and the parity
    assertion is what goes red.

    A command carrying `--test` is a gate's own self-test: it plants a probe in a
    throwaway tree rather than judging this one, so a script invoked only that
    way enforces nothing and is not a gate of its own.
    """
    workflow = yaml.safe_load(FAST_GATE.read_text(encoding="utf-8"))
    enforced: set[str] = set()
    for step in workflow["jobs"]["source-gates"]["steps"]:
        for line in str(step.get("run", "")).splitlines():
            for command in _COMMAND_SPLIT_RE.split(line):
                tokens = [token.strip("\"'`()") for token in command.split()]
                if "--test" in tokens:
                    continue
                for token in tokens:
                    match = _SCRIPT_TOKEN_RE.search(token)
                    if match and _HELPER_DIR not in token:
                        enforced.add(match.group(1))
    return enforced


def fast_gate_source_gate_steps() -> set[str]:
    # An undocumented script keeps its own path, so it reads as a workflow-only
    # entry below instead of vanishing from both sides of the comparison.
    return {_STEP_DOC_NAMES.get(script, script) for script in fast_gate_source_gate_scripts()}


class TestFastGateDocParity:
    def test_comment_history_gate_is_documented(self) -> None:
        assert "comment-history-lint" in fast_gate_source_gate_steps()
        assert "comment-history-lint" in documented_gate_steps()

    def test_doc_table_matches_workflow_jobs(self) -> None:
        workflow = fast_gate_jobs()
        documented = documented_gate_jobs()
        assert documented == workflow, (
            "docs table drift: workflow-only="
            f"{sorted(workflow - documented)} doc-only={sorted(documented - workflow)}"
        )

    def test_step_table_matches_the_gates_source_gates_runs(self) -> None:
        # The job table says there are fourteen gates; this is the table that
        # names them, so a gate added to or dropped from the job must move a
        # row here too.
        workflow = fast_gate_source_gate_steps()
        documented = documented_gate_steps()
        assert documented == workflow, (
            "gate-step table drift: workflow-only="
            f"{sorted(workflow - documented)} doc-only={sorted(documented - workflow)}"
        )
        assert documented == set(_STEP_DOC_NAMES.values())
