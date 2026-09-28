"""Behavioural tests for .github/workflows/pr-merge-conflict-label.yml.

The sweep's logic is a bash + jq script in a `run:` block. These tests run it
for real with `gh` replaced by a stub, so two things are checked rather than
assumed:

* the label POST body is built by a jq program every jq release accepts --
  `label` is a jq keyword, and jq 1.6 (the CodeBuild fleet's jq) refused
  `--arg label`, so every add failed with HTTP 422;
* PRs already CONFLICTING/MERGEABLE are settled BEFORE the wait, so a push
  that cancels the run mid-wait loses nothing;
* UNKNOWN mergeability costs ONE re-list of the open PRs, never a per-PR
  `gh pr view`; a PR still UNKNOWN, or a failed re-list, leaves it untouched.

Skipped where bash or jq is unavailable, and on Windows (stub PATH and chmod
semantics differ), matching test_issue_summary_workflow.py.
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

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "pr-merge-conflict-label.yml"
LABEL = "merge conflict"

# Every word jq 1.6's lexer reserves. A `--arg <keyword>` binding compiles on
# jq 1.7+ but is a syntax error on 1.6, so a local jq 1.7 cannot catch it.
JQ_KEYWORDS = (
    "__loc__ and as break catch def elif else end foreach if import include "
    "label module or reduce then try"
).split()
ARG_BINDING = re.compile(r"--(?:arg|argjson|slurpfile|rawfile)\s+([A-Za-z_]\w*)\s")

needs_posix = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None or shutil.which("jq") is None,
    reason="requires a POSIX bash and jq",
)

GH_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
log="$STUB_DIR/calls.log"
printf '%s\n' "$*" >>"$log"
case "$1 $2" in
  "label list") printf '%s\n' "$LABEL" ;;
  "label create") ;;
  "pr list")
    n="$(grep -c '^pr list' "$log")"
    if [ ! -f "$STUB_DIR/list$n.json" ]; then echo "listing failed" >&2; exit 1; fi
    cat "$STUB_DIR/list$n.json"
    ;;
  "pr view") echo "unexpected per-PR view" >&2; exit 99 ;;
  "api --method")
    if [ "$3" = "POST" ]; then cat >>"$STUB_DIR/post_bodies.log"; fi
    ;;
  *) echo "unhandled gh call: $*" >&2; exit 98 ;;
esac
"""


def _script() -> str:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    (step,) = doc["jobs"]["label"]["steps"]
    return step["run"]


def _pr(number: int, mergeable: str, labelled: bool) -> dict:
    return {
        "number": number,
        "mergeable": mergeable,
        "labels": [{"name": LABEL}] if labelled else [],
    }


def _sweep(tmp_path: Path, *listings: list[dict]) -> tuple[list[str], list[dict], str]:
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    gh = stub_dir / "gh"
    gh.write_text(GH_STUB, encoding="utf-8")
    gh.chmod(0o755)
    for i, listing in enumerate(listings, start=1):
        (stub_dir / f"list{i}.json").write_text(json.dumps(listing), encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_DIR": str(stub_dir),
        "GH_TOKEN": "stub",
        "REPO": "owner/repo",
        "LABEL": LABEL,
        "PR_LIST_LIMIT": "900",
        "RECHECK_DELAY_SECONDS": "0",
    }
    work = tmp_path / "work"
    work.mkdir()
    proc = subprocess.run(
        ["bash", "-c", _script()],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    calls = (stub_dir / "calls.log").read_text(encoding="utf-8").splitlines()
    bodies_file = stub_dir / "post_bodies.log"
    bodies = []
    if bodies_file.exists():
        decoder = json.JSONDecoder()
        text = bodies_file.read_text(encoding="utf-8").strip()
        while text:
            obj, end = decoder.raw_decode(text)
            bodies.append(obj)
            text = text[end:].strip()
    return calls, bodies, proc.stdout + proc.stderr


@pytest.mark.parametrize("name", ["pr-merge-conflict-label.yml", "fork-pr-label.yml"])
def test_label_workflows_bind_no_jq_keyword(name: str) -> None:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    bound = ARG_BINDING.findall(text)
    assert bound, f"{name}: expected at least one jq --arg binding"
    assert not set(bound) & set(JQ_KEYWORDS), f"{name}: jq keyword bound as a variable: {bound}"


@needs_posix
def test_definitive_states_add_and_remove_the_label(tmp_path: Path) -> None:
    calls, bodies, _ = _sweep(
        tmp_path,
        [
            _pr(1, "CONFLICTING", False),  # add
            _pr(2, "CONFLICTING", True),  # already labelled: no call
            _pr(3, "MERGEABLE", True),  # remove
            _pr(4, "MERGEABLE", False),  # nothing to do
        ],
    )
    assert bodies == [{"labels": [LABEL]}]
    assert "api --method POST repos/owner/repo/issues/1/labels --input -" in calls
    assert "api --method DELETE repos/owner/repo/issues/3/labels/merge%20conflict" in calls
    mutations = [c for c in calls if c.startswith("api --method")]
    assert len(mutations) == 2
    # No UNKNOWN in the first listing -> no second listing.
    assert sum(c.startswith("pr list") for c in calls) == 1


@needs_posix
def test_unknown_costs_one_relist_not_a_view_per_pr(tmp_path: Path) -> None:
    first = [_pr(90, "CONFLICTING", False)] + [_pr(n, "UNKNOWN", n % 2 == 0) for n in range(1, 41)]
    second = [
        _pr(90, "MERGEABLE", True),  # settled in pass one: not touched again
        _pr(1, "CONFLICTING", False),  # settled: add
        _pr(2, "MERGEABLE", True),  # settled: remove
        _pr(3, "UNKNOWN", False),  # still unknown: untouched
        _pr(4, "UNKNOWN", True),  # still unknown: label kept
    ]
    calls, bodies, out = _sweep(tmp_path, first, second)
    lists = [i for i, c in enumerate(calls) if c.startswith("pr list")]
    assert len(lists) == 2
    assert not any(c.startswith("pr view") for c in calls)
    mutations = [(i, c) for i, c in enumerate(calls) if c.startswith("api --method")]
    assert [c for _, c in mutations] == [
        "api --method POST repos/owner/repo/issues/90/labels --input -",
        "api --method POST repos/owner/repo/issues/1/labels --input -",
        "api --method DELETE repos/owner/repo/issues/2/labels/merge%20conflict",
    ]
    # The already-known PR is labelled before the second listing (the wait).
    assert mutations[0][0] < lists[1] < mutations[1][0]
    assert bodies == [{"labels": [LABEL]}, {"labels": [LABEL]}]
    assert "PR #4: mergeability UNKNOWN -> unchanged" in out


@needs_posix
def test_failed_relist_keeps_first_pass_and_exits_clean(tmp_path: Path) -> None:
    calls, bodies, out = _sweep(
        tmp_path,
        [_pr(1, "CONFLICTING", False), _pr(2, "UNKNOWN", True)],
        # no second listing file: the stub fails the re-list
    )
    assert [c for c in calls if c.startswith("api --method")] == [
        "api --method POST repos/owner/repo/issues/1/labels --input -",
    ]
    assert "Re-listing open PRs failed" in out
