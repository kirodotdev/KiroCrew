from __future__ import annotations

import re
from pathlib import Path

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
    text = GATE_DOC.read_text(encoding="utf-8").splitlines()
    jobs: set[str] = set()
    in_section = False
    in_table = False
    for line in text:
        if line.startswith("## `fast-gate.yml`"):
            in_section = True
            continue
        if in_section and not in_table:
            if line.startswith("| Job |"):
                in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            match = _DOC_ROW_RE.match(line)
            if match and match.group(1) != "Job":
                jobs.add(match.group(1))
    return jobs


class TestFastGateDocParity:
    def test_comment_history_gate_is_documented(self) -> None:
        assert "comment-history-lint" in fast_gate_jobs()
        assert "comment-history-lint" in documented_gate_jobs()

    def test_doc_table_matches_workflow_jobs(self) -> None:
        workflow = fast_gate_jobs()
        documented = documented_gate_jobs()
        assert documented == workflow, (
            "docs table drift: workflow-only="
            f"{sorted(workflow - documented)} doc-only={sorted(documented - workflow)}"
        )
