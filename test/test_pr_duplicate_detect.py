"""Behavioural tests for .github/workflows/pr-duplicate-detect.yml plus unit
tests for its shared engine scripts/detect_pr_overlap.py.

The workflow's two lanes share ONE detection engine and differ only in what they
do with its answer, so the engine is unit-tested directly (importlib against
canned JSON, no network, mirroring test/test_update_contributors.py) and each
lane's `run:` block is extracted and executed for real with `gh` replaced by a
stub, so the comment/close DECISIONS and their idempotency are verified rather
than assumed. The decision is the whole point: too broad and every stacked PR
gets a false duplicate warning, too narrow and the duplicated effort the
proposals target slips through.

Both conditions must hold to flag -- same referenced issue AND a non-empty
changed-file intersection -- and the triggering PR is never matched against
itself; these tests pin exactly that.

Skipped where the POSIX toolchain the steps need (bash, jq) or python3.12 is
unavailable, e.g. the Windows leg of the matrix. Mirrors the nt guard in
test_pr_readiness_sweep.py / test_fork_review_heal.py.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import pytest
except ModuleNotFoundError:  # pragma: no cover - only when run as the __main__ driver
    pytest = None  # type: ignore[assignment]

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - PyYAML is absent in the sandbox
    yaml = None  # type: ignore[assignment]

_REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "pr-duplicate-detect.yml"
SCRIPT = _REPO_ROOT / "scripts" / "detect_pr_overlap.py"


def _py312() -> str | None:
    """The workflow pins python 3.12; the steps invoke `python3`. Prefer a real
    3.12 on PATH so the extracted step runs the same interpreter CI uses."""
    for cand in ("python3.12", "python3"):
        exe = shutil.which(cand)
        if exe is None:
            continue
        out = subprocess.run([exe, "--version"], capture_output=True, text=True)
        if "3.12" in (out.stdout + out.stderr):
            return exe
    return None


if pytest is not None:
    pytestmark = pytest.mark.skipif(
        not WORKFLOW.exists()
        or not SCRIPT.exists()
        or os.name == "nt"
        or shutil.which("bash") is None
        or shutil.which("jq") is None
        or _py312() is None,
        reason="requires the workflow + engine plus a POSIX bash, jq and python3.12",
    )


# --------------------------------------------------------------------------------
# Engine unit tests (importlib, no network).
# --------------------------------------------------------------------------------


def _load_engine():
    spec = importlib.util.spec_from_file_location("detect_pr_overlap", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["detect_pr_overlap"] = module
    spec.loader.exec_module(module)
    return module


def test_engine_same_issue_intersecting_files_is_flagged() -> None:
    eng = _load_engine()
    subject = {"number": 10, "body": "Fixes #1", "state": "OPEN",
               "files": ["a.py", "b.py"], "closingIssues": [1]}
    cand = {"number": 5, "body": "Closes #1", "state": "OPEN",
            "files": ["a.py", "c.py"], "closingIssues": []}
    res = eng.analyze({"subject": subject, "candidates": [cand]})
    assert res["has_overlap"] is True
    assert [o["number"] for o in res["overlaps"]] == [5]
    assert res["overlaps"][0]["files"] == ["a.py"]
    assert res["overlaps"][0]["file_count"] == 1
    assert res["overlaps"][0]["exact"] is False


def test_engine_disjoint_files_not_flagged() -> None:
    eng = _load_engine()
    subject = {"number": 10, "body": "Fixes #1", "state": "OPEN", "files": ["a.py"],
               "closingIssues": [1]}
    cand = {"number": 5, "body": "Fixes #1", "state": "OPEN", "files": ["z.py"],
            "closingIssues": [1]}
    assert eng.analyze({"subject": subject, "candidates": [cand]})["overlaps"] == []


def test_engine_different_issue_not_flagged() -> None:
    eng = _load_engine()
    subject = {"number": 10, "body": "Fixes #1", "state": "OPEN", "files": ["a.py"],
               "closingIssues": [1]}
    cand = {"number": 5, "body": "Fixes #2", "state": "OPEN", "files": ["a.py"],
            "closingIssues": [2]}
    assert eng.analyze({"subject": subject, "candidates": [cand]})["overlaps"] == []


def test_engine_never_matches_self() -> None:
    eng = _load_engine()
    subject = {"number": 10, "body": "Fixes #1", "state": "OPEN", "files": ["a.py"],
               "closingIssues": [1]}
    assert eng.analyze({"subject": subject, "candidates": [subject]})["overlaps"] == []


def test_engine_only_open_candidates() -> None:
    eng = _load_engine()
    subject = {"number": 10, "body": "Fixes #1", "state": "OPEN", "files": ["a.py"],
               "closingIssues": [1]}
    closed = {"number": 5, "body": "Fixes #1", "state": "CLOSED", "files": ["a.py"],
              "closingIssues": [1]}
    assert eng.analyze({"subject": subject, "candidates": [closed]})["overlaps"] == []


def test_engine_body_keyword_fallback_when_linkage_empty() -> None:
    eng = _load_engine()
    # closingIssues empty; only the body "Resolves #3" links the issue.
    subject = {"number": 10, "body": "Resolves #3", "state": "OPEN", "files": ["a.py"],
               "closingIssues": []}
    cand = {"number": 5, "body": "Fixes #3", "state": "OPEN", "files": ["a.py"],
            "closingIssues": []}
    res = eng.analyze({"subject": subject, "candidates": [cand]})
    assert [o["number"] for o in res["overlaps"]] == [5]


def test_engine_exact_set_match_is_a_stronger_signal() -> None:
    eng = _load_engine()
    subject = {"number": 10, "body": "Fixes #1", "state": "OPEN", "files": ["a.py", "b.py"],
               "closingIssues": [1]}
    cand = {"number": 5, "body": "Fixes #1", "state": "OPEN", "files": ["b.py", "a.py"],
            "closingIssues": [1]}
    res = eng.analyze({"subject": subject, "candidates": [cand]})
    assert res["overlaps"][0]["exact"] is True


# --------------------------------------------------------------------------------
# Workflow step extraction + behavioural execution against a `gh` stub.
# --------------------------------------------------------------------------------

# The `gh` stub serves the reads the engine's --fetch path makes (pr view, pr
# list, api graphql) from per-PR fixtures, and RECORDS every mutating call
# (comment create/patch/delete, pr comment, pr close) to files the test asserts
# on. Read shapes are keyed on argv; the graphql closingIssues come from the
# per-PR fixture too.
GH_STUB = r"""#!/usr/bin/env bash
set -euo pipefail

# --- pr list --state open --json number ---
if [ "$1 ${2:-}" = "pr list" ]; then
  cat "$FIXTURES/open_prs.json"
  exit 0
fi

# --- pr view <n> --json number,body,state,files ---
if [ "$1 ${2:-}" = "pr view" ]; then
  cat "$FIXTURES/pr_$3.json"
  exit 0
fi

# --- pr comment <n> --body ... (on-merge pointer) ---
if [ "$1 ${2:-}" = "pr comment" ]; then
  printf 'comment %s\n' "$3" >> "$FIXTURES/pr_comment.txt"
  exit 0
fi

# --- pr close <n> (on-merge) ---
if [ "$1 ${2:-}" = "pr close" ]; then
  printf 'close %s\n' "$3" >> "$FIXTURES/pr_close.txt"
  exit 0
fi

if [ "$1" = "api" ]; then
  method="GET"
  target=""
  jqexpr=""
  for ((i=1; i<=$#; i++)); do
    a="${!i}"
    case "$a" in
      --method) j=$((i+1)); method="${!j}" ;;
      --jq) j=$((i+1)); jqexpr="${!j}" ;;
      graphql) target="graphql" ;;
      repos/*) target="$a" ;;
    esac
  done

  # Emit a fixture, applying the request's --jq like real `gh` does.
  emit() {
    if [ -n "$jqexpr" ]; then
      jq -r "$jqexpr" "$1"
    else
      cat "$1"
    fi
  }

  if [ "$target" = "graphql" ]; then
    # Return the closingIssues fixture for the queried PR number (-F number=N).
    num=""
    for ((i=1; i<=$#; i++)); do
      a="${!i}"
      case "$a" in number=*) num="${a#number=}" ;; esac
    done
    emit "$FIXTURES/graphql_$num.json"
    exit 0
  fi

  case "$method:$target" in
    GET:repos/*/comments*)
      # List existing marker comments for the subject PR.
      emit "$FIXTURES/comments.json"
      exit 0 ;;
    POST:repos/*/comments*|POST:repos/*/issues/*/comments)
      printf 'POST %s\n' "$target" >> "$FIXTURES/comment_write.txt" ;;
    PATCH:repos/*/comments*)
      printf 'PATCH %s\n' "$target" >> "$FIXTURES/comment_write.txt" ;;
    DELETE:repos/*/comments*)
      printf 'DELETE %s\n' "$target" >> "$FIXTURES/comment_write.txt" ;;
    *)
      # Fall through: POST/PATCH to issues/<n>/comments matches the first case.
      case "$target" in
        repos/*/comments*|repos/*)
          printf '%s %s\n' "$method" "$target" >> "$FIXTURES/comment_write.txt" ;;
        *)
          echo "gh stub: unhandled api $method $target" >&2; exit 91 ;;
      esac ;;
  esac
  exit 0
fi

echo "gh stub: unhandled: $*" >&2
exit 90
"""


def _extract_run_blocks_stdlib(text: str) -> list[str]:
    """PyYAML-free `run: |` literal-block extractor for the __main__ driver."""
    lines = text.splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("run: |"):
            key_indent = len(lines[i]) - len(lines[i].lstrip(" "))
            body: list[str] = []
            body_indent: int | None = None
            j = i + 1
            while j < len(lines):
                ln = lines[j]
                if ln.strip() == "":
                    body.append("")
                    j += 1
                    continue
                ind = len(ln) - len(ln.lstrip(" "))
                if ind <= key_indent:
                    break
                if body_indent is None:
                    body_indent = ind
                body.append(ln[body_indent:] if len(ln) >= body_indent else ln.lstrip(" "))
                j += 1
            while body and body[-1] == "":
                body.pop()
            blocks.append("\n".join(body))
            i = j
            continue
        i += 1
    return blocks


def _step(job: str, marker: str) -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    if yaml is not None:
        spec = yaml.safe_load(text)
        runs = [s["run"] for s in spec["jobs"][job]["steps"] if "run" in s]
    else:  # pragma: no cover - sandbox PyYAML-free fallback
        runs = [b for b in _extract_run_blocks_stdlib(text) if marker in b]
    matches = [r for r in runs if marker in r]
    assert len(matches) == 1, f"expected one {job} step matching {marker!r}, got {len(matches)}"
    return matches[0]


def on_open_step() -> str:
    return _step("on-open", "Find any comment this lane previously left")


def on_merge_step() -> str:
    return _step("on-merge", "Close each overlapping open PR with a pointer")


class Harness:
    """Runs one lane's step against one fixture repository state."""

    def __init__(self, root: Path) -> None:
        self.fixtures = root / "fixtures"
        self.work = _REPO_ROOT  # steps cd nowhere; scripts/ path is repo-relative
        bindir = root / "bin"
        for d in (self.fixtures, bindir):
            d.mkdir(parents=True)
        stub = bindir / "gh"
        stub.write_text(GH_STUB)
        stub.chmod(0o755)
        self.bindir = bindir
        self.py = _py312() or sys.executable

    def _write_prs(self, subject: dict, candidates: list[dict]) -> None:
        all_prs = [subject, *candidates]
        (self.fixtures / "open_prs.json").write_text(
            json.dumps([{"number": p["number"]} for p in all_prs
                        if (p.get("state") or "OPEN").upper() == "OPEN"])
        )
        for p in all_prs:
            (self.fixtures / f"pr_{p['number']}.json").write_text(
                json.dumps({
                    "number": p["number"],
                    "body": p.get("body", ""),
                    "state": p.get("state", "OPEN"),
                    "files": [{"path": f} for f in p.get("files", [])],
                })
            )
            (self.fixtures / f"graphql_{p['number']}.json").write_text(
                json.dumps({"data": {"repository": {"pullRequest": {
                    "closingIssuesReferences": {
                        "nodes": [{"number": n} for n in p.get("closingIssues", [])]
                    }}}}})
            )

    def _env(self) -> dict:
        # The step invokes `python3`; front-load a python3 -> python3.12 shim so
        # the engine runs under 3.12 (the `from __future__` + typing is fine on
        # 3.9 too, but pin the version CI uses).
        return {
            **os.environ,
            "PATH": f"{self.bindir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(self.fixtures),
            "GH_TOKEN": "x",
            "REPO": "kirodotdev/KiroCrew",
        }

    def run_on_open(self, script: str, subject: dict, candidates: list[dict],
                    existing_comment_id: int | None = None) -> subprocess.CompletedProcess:
        self._write_prs(subject, candidates)
        comments = ([{"id": existing_comment_id, "body": "<!-- pr-duplicate-detect --> old"}]
                    if existing_comment_id is not None else [])
        (self.fixtures / "comments.json").write_text(json.dumps(comments))
        for f in ("comment_write.txt", "pr_comment.txt", "pr_close.txt"):
            (self.fixtures / f).unlink(missing_ok=True)
        env = {**self._env(), "PR_NUMBER": str(subject["number"]),
               "MARKER": "<!-- pr-duplicate-detect -->"}
        # Ensure `python3` resolves to a 3.12; put a shim in bindir if needed.
        self._ensure_python3_shim()
        return subprocess.run(  # noqa: S603 - fixed argv, test-local stub
            ["bash", "-c", script], cwd=self.work, env=env, text=True, capture_output=True)

    def run_on_merge(self, script: str, subject: dict, candidates: list[dict],
                     existing_comment_id: int | None = None) -> subprocess.CompletedProcess:
        self._write_prs(subject, candidates)
        comments = ([{"id": existing_comment_id, "body": "<!-- pr-duplicate-coauthor --> old"}]
                    if existing_comment_id is not None else [])
        (self.fixtures / "comments.json").write_text(json.dumps(comments))
        for f in ("comment_write.txt", "pr_comment.txt", "pr_close.txt"):
            (self.fixtures / f).unlink(missing_ok=True)
        env = {**self._env(), "PR_NUMBER": str(subject["number"]),
               "COAUTHOR_MARKER": "<!-- pr-duplicate-coauthor -->"}
        self._ensure_python3_shim()
        return subprocess.run(  # noqa: S603 - fixed argv, test-local stub
            ["bash", "-c", script], cwd=self.work, env=env, text=True, capture_output=True)

    def _ensure_python3_shim(self) -> None:
        shim = self.bindir / "python3"
        if self.py != shutil.which("python3"):
            shim.write_text(f'#!/usr/bin/env bash\nexec "{self.py}" "$@"\n')
            shim.chmod(0o755)

    def reads(self, name: str) -> list[str]:
        f = self.fixtures / name
        return f.read_text().splitlines() if f.exists() else []


# ── on-open: overlap -> exactly one comment naming the earlier PR ─────────────


def test_on_open_overlap_posts_one_comment(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    subject = {"number": 10, "body": "Fixes #1", "files": ["a.py", "b.py"], "closingIssues": [1]}
    earlier = {"number": 5, "body": "Fixes #1", "files": ["a.py"], "closingIssues": [1]}
    proc = h.run_on_open(on_open_step(), subject, [earlier])
    assert proc.returncode == 0, proc.stderr
    writes = h.reads("comment_write.txt")
    assert len(writes) == 1 and writes[0].startswith("POST"), writes
    # The engine's JSON (echoed by the step) names the earlier PR #5 as the overlap.
    assert '"number": 5' in proc.stdout


def test_on_open_disjoint_files_posts_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    subject = {"number": 10, "body": "Fixes #1", "files": ["a.py"], "closingIssues": [1]}
    other = {"number": 5, "body": "Fixes #1", "files": ["z.py"], "closingIssues": [1]}
    proc = h.run_on_open(on_open_step(), subject, [other])
    assert proc.returncode == 0, proc.stderr
    assert h.reads("comment_write.txt") == []


def test_on_open_different_issue_posts_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    subject = {"number": 10, "body": "Fixes #1", "files": ["a.py"], "closingIssues": [1]}
    other = {"number": 5, "body": "Fixes #2", "files": ["a.py"], "closingIssues": [2]}
    proc = h.run_on_open(on_open_step(), subject, [other])
    assert proc.returncode == 0, proc.stderr
    assert h.reads("comment_write.txt") == []


def test_on_open_is_idempotent_updates_in_place(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    subject = {"number": 10, "body": "Fixes #1", "files": ["a.py"], "closingIssues": [1]}
    earlier = {"number": 5, "body": "Fixes #1", "files": ["a.py"], "closingIssues": [1]}
    # A prior marker comment (id 999) already exists -> the re-run PATCHes it.
    proc = h.run_on_open(on_open_step(), subject, [earlier], existing_comment_id=999)
    assert proc.returncode == 0, proc.stderr
    writes = h.reads("comment_write.txt")
    assert len(writes) == 1 and writes[0].startswith("PATCH"), writes


def test_on_open_removes_stale_comment_when_no_overlap(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    subject = {"number": 10, "body": "Fixes #1", "files": ["a.py"], "closingIssues": [1]}
    other = {"number": 5, "body": "Fixes #1", "files": ["z.py"], "closingIssues": [1]}
    proc = h.run_on_open(on_open_step(), subject, [other], existing_comment_id=999)
    assert proc.returncode == 0, proc.stderr
    writes = h.reads("comment_write.txt")
    assert len(writes) == 1 and writes[0].startswith("DELETE"), writes


# ── on-merge: overlap -> close the earlier PR with a pointer ───────────────────


def test_on_merge_closes_overlapping_open_pr(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    merged = {"number": 10, "body": "Fixes #1", "state": "MERGED",
              "files": ["a.py", "b.py"], "closingIssues": [1]}
    earlier = {"number": 5, "body": "Fixes #1", "state": "OPEN",
               "files": ["a.py"], "closingIssues": [1]}
    proc = h.run_on_merge(on_merge_step(), merged, [earlier])
    assert proc.returncode == 0, proc.stderr
    assert h.reads("pr_close.txt") == ["close 5"]
    assert h.reads("pr_comment.txt") == ["comment 5"]
    # Co-authorship recorded on the merged PR.
    assert any(w.startswith("POST") for w in h.reads("comment_write.txt"))


def test_on_merge_leaves_non_overlapping_prs_untouched(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    merged = {"number": 10, "body": "Fixes #1", "state": "MERGED",
              "files": ["a.py"], "closingIssues": [1]}
    disjoint = {"number": 5, "body": "Fixes #1", "state": "OPEN",
                "files": ["z.py"], "closingIssues": [1]}
    other_issue = {"number": 6, "body": "Fixes #2", "state": "OPEN",
                   "files": ["a.py"], "closingIssues": [2]}
    proc = h.run_on_merge(on_merge_step(), merged, [disjoint, other_issue])
    assert proc.returncode == 0, proc.stderr
    assert h.reads("pr_close.txt") == []
    assert h.reads("pr_comment.txt") == []


# ── the workflow never checks out or executes PR head code ────────────────────


def test_workflow_never_executes_pr_head_code() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    # No PR-head checkout: checkout steps must not pass the PR ref, and there is
    # no build/install of PR code. The engine only reads metadata via `gh`.
    assert "ref: ${{ github.event.pull_request.head" not in text
    for forbidden in ("git clone", "npm ci", "npm install", "pip install", "make build"):
        assert forbidden not in text, f"must not build/execute PR code: {forbidden}"


# ── Standalone driver so the same assertions run without pytest ───────────────
if __name__ == "__main__":
    import tempfile

    if (
        os.name == "nt"
        or shutil.which("bash") is None
        or shutil.which("jq") is None
        or _py312() is None
        or not WORKFLOW.exists()
        or not SCRIPT.exists()
    ):
        print("SKIP: requires the workflow + engine plus a POSIX bash, jq and python3.12")
        raise SystemExit(0)

    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"{'ok  ' if cond else 'FAIL'} {name}")
        if not cond:
            failures.append(name)

    # Engine unit checks.
    eng = _load_engine()
    subj = {"number": 10, "body": "Fixes #1", "state": "OPEN", "files": ["a.py", "b.py"],
            "closingIssues": [1]}
    c_int = {"number": 5, "body": "Closes #1", "state": "OPEN", "files": ["a.py", "c.py"],
             "closingIssues": []}
    c_disj = {"number": 6, "body": "Fixes #1", "state": "OPEN", "files": ["z.py"],
              "closingIssues": [1]}
    c_diff = {"number": 7, "body": "Fixes #2", "state": "OPEN", "files": ["a.py"],
              "closingIssues": [2]}
    c_closed = {"number": 8, "body": "Fixes #1", "state": "CLOSED", "files": ["a.py"],
                "closingIssues": [1]}
    r = eng.analyze({"subject": subj, "candidates": [c_int, c_disj, c_diff, c_closed]})
    nums = [o["number"] for o in r["overlaps"]]
    check("engine_intersect_flagged", nums == [5])
    check("engine_disjoint_excluded", 6 not in nums)
    check("engine_diff_issue_excluded", 7 not in nums)
    check("engine_closed_excluded", 8 not in nums)
    check("engine_self_excluded", eng.analyze({"subject": subj, "candidates": [subj]})["overlaps"] == [])

    on_open = on_open_step()
    on_merge = on_merge_step()

    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "a")
        p = h.run_on_open(on_open, {"number": 10, "body": "Fixes #1", "files": ["a.py", "b.py"],
                                    "closingIssues": [1]},
                          [{"number": 5, "body": "Fixes #1", "files": ["a.py"], "closingIssues": [1]}])
        w = h.reads("comment_write.txt")
        check("on_open_overlap_posts_one",
              p.returncode == 0 and len(w) == 1 and w[0].startswith("POST"))

        h = Harness(Path(td) / "b")
        p = h.run_on_open(on_open, {"number": 10, "body": "Fixes #1", "files": ["a.py"],
                                    "closingIssues": [1]},
                          [{"number": 5, "body": "Fixes #1", "files": ["z.py"], "closingIssues": [1]}])
        check("on_open_disjoint_no_comment", p.returncode == 0 and h.reads("comment_write.txt") == [])

        h = Harness(Path(td) / "c")
        p = h.run_on_open(on_open, {"number": 10, "body": "Fixes #1", "files": ["a.py"],
                                    "closingIssues": [1]},
                          [{"number": 5, "body": "Fixes #1", "files": ["a.py"], "closingIssues": [1]}],
                          existing_comment_id=999)
        w = h.reads("comment_write.txt")
        check("on_open_idempotent_patch", p.returncode == 0 and len(w) == 1 and w[0].startswith("PATCH"))

        h = Harness(Path(td) / "d")
        p = h.run_on_open(on_open, {"number": 10, "body": "Fixes #1", "files": ["a.py"],
                                    "closingIssues": [1]},
                          [{"number": 5, "body": "Fixes #1", "files": ["z.py"], "closingIssues": [1]}],
                          existing_comment_id=999)
        w = h.reads("comment_write.txt")
        check("on_open_removes_stale", p.returncode == 0 and len(w) == 1 and w[0].startswith("DELETE"))

        h = Harness(Path(td) / "e")
        p = h.run_on_merge(on_merge, {"number": 10, "body": "Fixes #1", "state": "MERGED",
                                      "files": ["a.py", "b.py"], "closingIssues": [1]},
                           [{"number": 5, "body": "Fixes #1", "state": "OPEN",
                             "files": ["a.py"], "closingIssues": [1]}])
        check("on_merge_closes_earlier",
              p.returncode == 0 and h.reads("pr_close.txt") == ["close 5"]
              and h.reads("pr_comment.txt") == ["comment 5"])

        h = Harness(Path(td) / "f")
        p = h.run_on_merge(on_merge, {"number": 10, "body": "Fixes #1", "state": "MERGED",
                                      "files": ["a.py"], "closingIssues": [1]},
                           [{"number": 5, "body": "Fixes #1", "state": "OPEN",
                             "files": ["z.py"], "closingIssues": [1]}])
        check("on_merge_leaves_disjoint",
              p.returncode == 0 and h.reads("pr_close.txt") == [])

    if failures:
        print(f"\n{len(failures)} check(s) FAILED: {failures}")
        raise SystemExit(1)
    print("\nall checks passed")
