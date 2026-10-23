"""``.github/scripts/proof_line_check.py``: a fix states, inside ``## Tests``, whether its tests fail without it.

Pinned here: the line shapes and the closed list of reasons (green and red), that
the line must sit inside ``## Tests`` and nowhere else, the mode (warn now, red only
for pull requests opened on or after ``ENFORCE_FROM``), the CLI, and the wiring
(template text inside the existing ``## Tests`` comment, no new heading, a workflow
step that reads the script from the base ref). CI checks presence, place and shape;
a hand-typed ``PROVEN`` line passes it, and only the local comparison with
``prove.py``'s record (test_prepare_pr_prove_line.py) catches that.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from ai_review_workflows_helpers import _bash, _child_env
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "proof_line_check.py"
WORKFLOW = ROOT / ".github" / "workflows" / "code-review.yml"
TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
DESC_CHECK = ROOT / ".github" / "scripts" / "pr-description-check.sh"

mod = load_skill_script("proof_line_check_under_test", SCRIPT)

PROVEN = "Proof: PROVEN test/test_a.py::test_x (head 0123456789ab)"
NOT = "Proof: not provable -- test-only change"


def _body(tests: str, extra: str = "") -> str:
    return f"## Problem / Motivation\n\n**Goal:** x\n\n## Tests\n\n{tests}\n\n## Manual verification\n\nN/A\n{extra}"


def _problems(tests: str, title: str = "fix: stop the leak", extra: str = "") -> list[str]:
    return mod.check(title, _body(tests, extra))[0]


# ------------------------------------------------------------ green path


@pytest.mark.parametrize(
    "tests",
    [
        PROVEN,
        "test/test_a.py locks the boundary.\n\n" + PROVEN,
        "- " + PROVEN,
        "Proof: PROVEN test/test_a.py::test_x, test/test_a.py::test_y[a, b] (head 0123456789ab)",
        "Proof: not provable -- frontend test (prove.py runs Python only)",
        "Proof: not provable -- test-only change",
        "Proof: not provable -- copy-only change",
        "Proof: not provable -- docs or config only",
        "Proof: not provable -- prove.py timed out",
        "Proof: not provable -- other: the fix is a one-line data migration, tested by hand",
        "Proof: not provable -- TEST-ONLY CHANGE",
        "### Cases\n\n" + PROVEN,  # a sub-heading stays inside the section
    ],
)
def test_well_formed_lines_pass(tests: str) -> None:
    assert _problems(tests) == []


@pytest.mark.parametrize(
    "title", ["feat: x", "docs: x", "chore(deps): bump", "revert: x", "refactor: fix the thing"]
)
def test_other_titles_are_not_checked(title: str) -> None:
    assert mod.check(title, "## Tests\n\nnothing\n") == ([], None)


@pytest.mark.parametrize("title", ["fix: x", "fix(chat): x", "fix!: x", "Fix: x"])
def test_fix_titles_are_checked(title: str) -> None:
    assert mod.check(title, "## Tests\n\nnothing\n")[0]


def test_a_proven_line_is_returned_for_the_record_comparison() -> None:
    problems, proven = mod.check("fix: x", _body(PROVEN))
    assert problems == []
    assert proven.group("sha") == "0123456789ab"
    assert proven.group("tests") == "test/test_a.py::test_x"
    problems, not_provable = mod.check("fix: x", _body(NOT))
    assert problems == [] and not_provable.re is mod.NOT_PROVABLE_RE  # compared too
    assert mod.check("fix: x", _body("Added tests."))[1] is None


# -------------------------------------------------------------- red path


def test_no_tests_section_is_one_finding_that_shows_the_shapes() -> None:
    (finding,) = mod.check("fix: x", "## What changed\n\nx\n")[0]
    assert "'## Tests'" in finding and "Proof: PROVEN" in finding and "not provable" in finding


def test_a_tests_section_with_no_line_is_reported() -> None:
    (finding,) = _problems("Added test_a.py, which locks the boundary.")
    assert "no Proof line" in finding


def test_the_unfilled_template_does_not_pass() -> None:
    body = TEMPLATE.read_text(encoding="utf-8")
    (finding,) = mod.check("fix: x", body)[0]
    assert "no Proof line" in finding


def test_a_fenced_example_or_comment_does_not_count() -> None:
    assert _problems(f"```\n{PROVEN}\n```")
    assert _problems(f"<!--\n{PROVEN}\n-->")


def test_a_line_outside_tests_is_reported_and_does_not_satisfy_the_check() -> None:
    problems = _problems("Added tests.", extra=f"\n## Pattern harvest\n\n{PROVEN}\n")
    assert len(problems) == 1 and "outside '## Tests'" in problems[0]


def test_a_line_outside_tests_does_not_hide_a_missing_one_inside() -> None:
    body = f"{PROVEN}\n\n## Tests\n\nAdded tests.\n"
    problems = mod.check("fix: x", body)[0]
    assert len(problems) == 1 and "outside '## Tests'" in problems[0]


def test_two_lines_inside_tests_are_reported() -> None:
    assert any("2 Proof lines" in p for p in _problems(PROVEN + "\n" + NOT))


@pytest.mark.parametrize(
    "line, fragment",
    [
        ("Proof: PROVEN", "does not parse"),
        ("Proof: PROVEN (head 0123456789ab)", "does not parse"),
        ("Proof: PROVEN test/test_a.py::t", "does not parse"),
        ("Proof: PROVEN test/test_a.py::t (head 0123)", "does not parse"),
        ("Proof: PROVEN test/test_a.py::t (head 0123456789AB)", "does not parse"),
        ("Proof: yes", "does not parse"),
        ("Proof: not provable", "does not parse"),
        ("Proof: not provable -- ", "does not parse"),
        ("Proof: not provable -- it is hard", "the reason must be one of"),
        ("Proof: not provable -- other: too short", "the reason must be one of"),
        ("Proof: not provable -- other:", "the reason must be one of"),
        ("Proof: not provable -- frontend test", "the reason must be one of"),
    ],
)
def test_malformed_lines_are_reported(line: str, fragment: str) -> None:
    problems = _problems(line)
    assert any(fragment in p for p in problems), problems


def test_other_needs_twenty_characters() -> None:
    assert not mod.reason_ok("other: " + "x" * 19)
    assert mod.reason_ok("other: " + "x" * 20)


# ------------------------------------------------------------------ mode


def test_ships_in_warn_mode() -> None:
    assert mod.ENFORCE_FROM is None
    assert mod.enforced("2030-01-01T00:00:00Z") is False
    assert mod.enforced(None) is False


@pytest.mark.parametrize(
    "created, expected",
    [
        ("2026-11-04T00:00:00Z", True),
        ("2026-11-05T09:30:00+00:00", True),
        ("2026-11-03T23:59:59Z", False),
    ],
)
def test_enforcement_applies_to_pull_requests_opened_on_or_after_the_date(
    created: str, expected: bool
) -> None:
    assert mod.enforced(created, enforce_from="2026-11-04") is expected


# ------------------------------------------------------------ CLI / env


def _run(title: str, body: str, **env: str) -> tuple[int, str]:
    full = {k: v for k, v in os.environ.items() if not k.startswith(("PR_", "PROVE", "TREE_"))}
    full.update({"PR_TITLE": title, "PR_BODY": body, "GITHUB_ACTIONS": ""})
    full.update(env)
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPT)],
        env=full,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_cli_green() -> None:
    rc, out = _run("fix: x", _body(PROVEN))
    assert rc == 0 and "Proof line present." in out


def test_cli_missing_line_warns_and_passes_in_warn_mode() -> None:
    rc, out = _run("fix: x", _body("Added tests."), GITHUB_ACTIONS="true")
    assert rc == 0
    assert out.startswith("::warning::Proof line: ")
    assert "advisory for now" in out


def test_cli_local_run_prints_plain_lines() -> None:
    rc, out = _run("fix: x", _body("Added tests."))
    assert rc == 0 and "PROOF LINE (warning):" in out and "::warning::" not in out


def test_cli_not_a_fix_is_skipped() -> None:
    rc, out = _run("feat: x", "")
    assert rc == 0 and "not required" in out


def test_cli_compares_a_proven_line_with_the_record_only_when_asked(tmp_path: Path) -> None:
    body = _body(PROVEN)
    rc, out = _run("fix: x", body)  # CI shape: no record, a hand-typed line passes
    assert rc == 0 and "Proof line present." in out
    rc, out = _run("fix: x", body, PROVE_RECORD=str(tmp_path / "none.json"), TREE_ID="t")
    assert rc == 0 and "no prove.py record" in out  # warn mode: printed, not failed


def test_enforce_mode_turns_a_finding_into_exit_one(monkeypatch, capsys) -> None:
    monkeypatch.setattr(mod, "ENFORCE_FROM", "2026-01-01")
    for key, value in {
        "PR_TITLE": "fix: x",
        "PR_BODY": _body("Added tests."),
        "PR_CREATED_AT": "2026-02-01T00:00:00Z",
        "GITHUB_ACTIONS": "true",
    }.items():
        monkeypatch.setenv(key, value)
    assert mod.main() == 1
    assert capsys.readouterr().out.startswith("::error::Proof line: ")
    monkeypatch.setenv("PR_CREATED_AT", "2025-12-31T00:00:00Z")  # opened before: still a warning
    assert mod.main() == 0
    monkeypatch.setenv("PR_CREATED_AT", "2026-02-01T00:00:00Z")
    monkeypatch.setenv("PR_BODY", _body(NOT))
    assert mod.main() == 0


# ---------------------------------------------------------------- wiring


def _steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["pr-hygiene"]["steps"]


def _step_index(fragment: str) -> int:
    for i, step in enumerate(_steps()):
        if fragment.lower() in str(step.get("name", "")).lower():
            return i
    raise AssertionError(f"no pr-hygiene step named like {fragment!r}")


def test_the_proof_text_lives_inside_the_existing_tests_section() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    headings = re.findall(r"^## .*$", text, re.MULTILINE)
    assert "## Proof" not in headings  # folded into ## Tests, no new heading
    block = text.split("\n## Tests\n", 1)[1].split("\n## ", 1)[0]
    assert "Proof: PROVEN <the tests that fail without the fix> (head <sha12>)" in block
    assert "Proof: not provable -- <reason>" in block
    for reason in mod.REASONS:
        assert reason in block
    # help text only: an unfilled template carries no uncommented Proof line
    assert not [
        ln for ln in re.sub(r"<!--.*?-->", "", block, flags=re.DOTALL).splitlines() if ln.strip()
    ]


def test_tests_stays_a_required_heading_and_proof_adds_none() -> None:
    required = DESC_CHECK.read_text(encoding="utf-8")
    assert "Tests" in required
    assert "Proof" not in required


def test_the_step_runs_after_pattern_harvest_and_reads_the_base_script() -> None:
    idx = _step_index("Proof line")
    assert idx > _step_index("Pattern harvest")
    step = _steps()[idx]
    run = step["run"]
    assert 'git show "$BASE_TIP:.github/scripts/proof_line_check.py"' in run
    assert "cp .github/scripts/proof_line_check.py" in run
    assert "${{" not in run  # untrusted text arrives through env only
    for key in ("PR_TITLE", "PR_BODY", "PR_CREATED_AT", "BASE_TIP"):
        assert key in step["env"]
    assert "::error::" not in run
    assert "continue-on-error" not in step


# ------------------------------------------- the forms an author really types


@pytest.mark.parametrize(
    "tests",
    [
        "* " + PROVEN,
        "+ " + PROVEN,
        "1. " + PROVEN,
        "2) " + NOT,
        f"`{PROVEN}`",
        f"- `{NOT}`",
        PROVEN + ".",
        "Proof: PROVEN test/test_a.py::test_x (head 0123456789ab).",
        "Proof: not provable \u2014 test-only change",  # em dash
        "Proof: not provable \u2013 test-only change.",  # en dash, trailing period
        "Proof: not provable -- test-only change.",
        "Proof:\tnot provable\t--\ttest-only change",  # tabs
        "Proof:  not  provable  --  test-only change",  # runs of spaces
        "Proof: Not Provable -- test-only change",
        "Proof: proven test/test_a.py::test_x (head 0123456789ab)",
        PROVEN + "\r",  # CRLF
    ],
)
def test_forms_an_author_types_pass(tests: str) -> None:
    assert _problems(tests) == []


@pytest.mark.parametrize(
    "line, fragment",
    [
        ("`Proof: not provable -- test-only change", "no Proof line"),  # unmatched backtick
        ("``Proof: not provable -- test-only change``", "no Proof line"),  # ONE pair only
        ("proof: not provable -- test-only change", "does not parse"),  # the keyword is exact
        ("Proof: not provable \u2014test-only change", "does not parse"),
        ("Proof: not provable -- other: too short.", "the reason must be one of"),
        ("Proof: not provable -- frontend test.", "the reason must be one of"),
    ],
)
def test_form_edges_stay_red(line: str, fragment: str) -> None:
    assert any(fragment in p for p in _problems(line)), _problems(line)


def test_a_quoted_line_is_not_a_proof_line() -> None:
    (finding,) = _problems("> " + PROVEN)
    assert "no Proof line" in finding
    assert _problems(PROVEN, extra=f"\n## Pattern harvest\n\n> {PROVEN}\n") == []


def test_a_proof_line_in_a_longer_fence_with_a_shorter_example_does_not_count() -> None:
    """A 4-backtick block holding a 3-backtick example of the line: the line is example text."""
    body = f"````markdown\n```\n{PROVEN}\n```\n````"
    (finding,) = _problems(body)
    assert "no Proof line" in finding
    assert _problems(f"~~~\n{PROVEN}\n~~~\n```\n{PROVEN}\n```") == [finding]
    assert _problems(f"```\n{PROVEN}\n``` not a close\n{PROVEN}\n```") == [finding]
    # and a real line after the block still counts
    assert _problems(f"````\n```\n{PROVEN}\n```\n````\n\n{NOT}") == []


@pytest.mark.parametrize(
    "tests",
    [
        f"<!-- ```\n-->\n```\n{PROVEN}\n```",  # the fence after the comment is a real one
        f"<!-- ```python -->\n{PROVEN}",  # ...but a fence marker inside a comment opens nothing
        f"```\n<!--\n```\n{PROVEN}",  # a comment marker in a fence opens nothing
        f"<!-- x --> {PROVEN}",  # the template check hides a line that starts a comment
        f"<!--\n{PROVEN}",  # an unclosed comment hides the rest
        f"```\n{PROVEN}",  # an unclosed fence hides the rest
    ],
)
def test_the_reader_hides_what_the_template_check_hides(tests: str) -> None:
    # Only a comment marker inside a fence is plain text; the fence then closes and the
    # line below it is visible. Every other shape hides the line.
    visible = tests.startswith(("```\n<!--", "<!-- ```python"))
    assert (_problems(tests) == []) is visible


def test_a_comment_inside_a_line_is_cut_out_of_it() -> None:
    assert _problems("Proof: not provable <!-- why --> -- test-only change") == []


def test_crlf_bodies_are_read_like_lf_bodies() -> None:
    body = _body(PROVEN).replace("\n", "\r\n")
    problems, parsed = mod.check("fix: x", body)
    assert problems == [] and parsed.string == PROVEN
    fenced = f"## Tests\r\n\r\n```\r\n{PROVEN}\r\n```\r\n"
    assert any("no Proof line" in p for p in mod.check("fix: x", fenced)[0])


# ---------------------------------------- `## Tests` is found as the template check finds it
# `pr-description-check.sh` decides whether `## Tests` is present, so this check must see
# the heading exactly as it does. The corpus is identical in test_sibling_list_check.py,
# which holds the other copy of the reader.

_CI_PREFIX = (
    "## Problem / Motivation\n\n**Goal:** x\n\n## Why it matters\n\nx\n\n"
    "## Not a goal\n\nx\n\n## What changed\n\nx\n\n"
)
_CI_BODIES = [
    "## Tests\n\nx\n",
    "## tests\n",
    "## Tests (new)\n",
    "## Testsuite\n",
    "## Tests ##\n",
    "##  Tests\n",
    "##\tTests\n",
    "### Tests\n",
    "# Tests\n",
    "   ## Tests\n",
    "    ## Tests\n",
    "> ## Tests\n",
    "text ## Tests\n",
    "```\n## Tests\n```\n",
    "```\n## Tests\n",
    "~~~\n## Tests\n~~~\n",
    "~~~\n```\n## Tests\n~~~\n",
    "````\n```\n## Tests\n```\n````\n",
    "```\n## Tests\n``` not a close\n## Tests\n```\n",
    "``` ```\n## Tests\n",
    "<!--\n## Tests\n-->\n",
    "<!-- ## Tests -->\n",
    "<!-- x --> ## Tests\n",
    "<!-- x --><!--\n## Tests\n-->\n",
    "text <!-- ## Tests\n",
    "<!--\n```\n-->\n## Tests\n",
    "```\n<!--\n```\n## Tests\n",
    "<!-- ```\n-->\n## Tests\n",
    "<!--\n## Tests\n",
    "## Tests\r\n\r\nx\r\n",
    "```\r\n## Tests\r\n```\r\n",
    "No tests section here\n",
    "",
]


def _ci_sees_tests(body: str, tmp_path: Path) -> bool:
    # A bare `bash` on a Windows runner is the WSL launcher, which neither runs the
    # script nor reads its Windows paths; `_bash()` resolves Git for Windows' Bash there.
    bash = _bash()
    assert bash, "the template check needs a Bash that reads native paths"
    out = tmp_path / "out"
    out.write_text("", encoding="utf-8")
    env = _child_env(
        {
            "PATH": os.environ.get("PATH", ""),
            "PR_BODY": _CI_PREFIX + body,
            "GITHUB_OUTPUT": str(out),
        }
    )
    subprocess.run([bash, str(DESC_CHECK)], env=env, check=True, capture_output=True)
    lines = out.read_text("utf-8").splitlines()
    annotation = next(ln for ln in lines if ln.startswith("annotation="))
    return "## Tests" not in annotation


@pytest.mark.parametrize("body", _CI_BODIES)
def test_tests_is_found_exactly_when_the_template_check_finds_it(body: str, tmp_path: Path) -> None:
    inside, _ = mod.split_sections(_CI_PREFIX + body)
    assert (inside is not None) is _ci_sees_tests(body, tmp_path), repr(body)


def test_a_sub_heading_stays_in_tests_and_a_peer_heading_ends_it() -> None:
    body = f"## Tests\n\n### Cases\n\n{PROVEN}\n\n## Manual verification\n\n{NOT}\n"
    problems = mod.check("fix: x", body)[0]
    assert len(problems) == 1 and "outside '## Tests'" in problems[0]


# ----------------------------------- the record decides the line (CLI, as the agent flow runs it)


def _write_record(tmp_path: Path, **fields: object) -> str:
    import json

    path = tmp_path / "record.json"
    record = {"tree": "t", "head": "0123456789abcdef", "verdict": "PROVEN", "tests": []}
    record.update(fields)
    path.write_text(json.dumps(record), encoding="utf-8")
    return str(path)


def test_cli_a_proven_record_refuses_a_not_provable_line(tmp_path: Path) -> None:
    record = _write_record(
        tmp_path, tests=["test/test_a.py::test_x"], line=PROVEN, verdict="PROVEN"
    )
    rc, out = _run("fix: x", _body(NOT), PROVE_RECORD=record, TREE_ID="t")
    assert rc == 0 and "prove.py recorded" in out and "PROOF LINE (warning)" in out
    rc, out = _run("fix: x", _body(PROVEN), PROVE_RECORD=record, TREE_ID="t")
    assert rc == 0 and "Proof line present." in out
    rc, out = _run("fix: x", _body(NOT), PROVE_RECORD=record, TREE_ID="another tree")
    assert rc == 0 and "Proof line present." in out  # a stale record decides nothing here


# ------------------------------- one reader: the merged sibling-sweep check is the source
# Both checks run as single files copied out of the base ref, so neither can import the
# other. The reader in `sibling_list_check.py` (merged first) is the source; this copy must
# stay identical to it, and this test fails the day one of them changes alone.

_READER_NAMES = (
    "HEADING_RE",
    "FENCE_RE",
    "COMMENT_OPEN_RE",
    "MARKER_RE",
    "_cut_comments",
    "visible_lines",
    "split_section",
)


def _definitions(path: Path) -> dict[str, str]:
    import ast

    text = path.read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for node in ast.parse(text).body:
        if isinstance(node, ast.FunctionDef):
            found[node.name] = ast.get_source_segment(text, node) or ""
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            found[node.targets[0].id] = ast.get_source_segment(text, node) or ""
    return found


@pytest.mark.parametrize("name", _READER_NAMES)
def test_the_body_reader_is_identical_to_the_sibling_sweep_checks(name: str) -> None:
    source = _definitions(ROOT / ".github" / "scripts" / "sibling_list_check.py")
    mine = _definitions(SCRIPT)
    assert name in source and name in mine
    assert mine[name] == source[name]
