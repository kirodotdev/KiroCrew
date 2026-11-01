"""``.github/scripts/sibling_list_check.py``: a fix lists the other places with its defect.

Pinned here: the parser (green and red shapes), the mode (warn now, red only for
pull requests opened on or after ``ENFORCE_FROM``), the end-to-end run against a
real two-commit repository, and the wiring (template block before ``## Pattern
harvest``, workflow step reading the script from the base ref, the section NOT
added to ``REQUIRED_SECTIONS``). The check proves shape and path existence, never
that a list is complete; nothing here pretends otherwise.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from git_maintenance_helpers import git_maintenance_off_entries
from named_cases_helpers import named_cases
from skill_script_helpers import load_skill_script

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "sibling_list_check.py"
WORKFLOW = ROOT / ".github" / "workflows" / "code-review.yml"
TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
DESC_CHECK = ROOT / ".github" / "scripts" / "pr-description-check.sh"

mod = load_skill_script("sibling_list_check_under_test", SCRIPT)

CHANGED = {"src/a.py", "src/old.py"}
TREE = {"src/a.py", "src/b.py", "src/c.py"}


def _exists(path: str) -> bool:
    return path in TREE


def _body(section: str) -> str:
    return f"## Tests\n\nx\n\n## Sibling sweep\n\n{section}\n\n## Pattern harvest\n\nNot generalizable: x\n"


def _check(section: str, title: str = "fix: stop the leak") -> list[str]:
    return mod.check(title, _body(section), CHANGED, _exists)


# ------------------------------------------------------------ green path


@pytest.mark.parametrize(
    "section",
    [
        *named_cases(
            "test_well_formed_sections_pass",
            "section",
            [
                "Siblings: none -- single call site, grep found one",
                "Swept: git grep -n redact\nSibling: src/a.py:12 -- changed here",
                "Sibling: src/b.py -- tracked in #123",
                "Sibling: src/b.py:7 -- not affected -- already guarded above",
                "- Sibling: src/b.py -- not affected -- already guarded above\n- Sibling: src/c.py -- tracked in #9",
                "Sibling: src/old.py -- changed here",  # a deleted file is in the diff, not the tree
                "SIBLINGS: NONE -- single call site",
            ],
        ),
        *named_cases(
            "test_documented_forms_pass",
            "section",
            [
                "Sibling: `src/a.py` -- changed here",
                "Sibling: `src/a.py:12` -- changed here",
                "Sibling: `src/a.py:10-20` -- changed here",
                "Sibling: src/a.py:10-20 -- changed here",
                "- Sibling: `src/b.py` -- tracked in #7",
                "* Sibling: `src/b.py` -- tracked in #7",
                "+ Sibling: src/b.py -- tracked in #7",
                "1. Sibling: `src/b.py:7-9` -- not affected -- already guarded above",
                "2) Sibling: src/b.py -- tracked in #7",
                "Sibling: src/a.py \u2014 changed here",  # em dash
                "Sibling: src/b.py \u2013 tracked in #7",  # en dash
                "Sibling: src/b.py \u2014 not affected \u2014 already guarded above",
                "Sibling: src/a.py -- changed here.",  # trailing period
                "Sibling: src/b.py -- tracked in #7.",
                "Siblings: none.  -- single call site",
                "Siblings: none \u2014 single call site",
                "- Siblings: none -- single call site.",
                "Sibling:\tsrc/a.py\t--\tchanged here",  # tabs
                "sibling: src/a.py -- Changed Here",  # case
                "Sibling : src/a.py -- changed here",
                "Sibling: src/a.py -- changed here\r\nSibling: `src/b.py` -- tracked in #9\r\n",  # CRLF
            ],
        ),
    ],
)
def test_the_section_passes(section: str) -> None:
    assert _check(section) == []


@pytest.mark.parametrize("title", ["feat: x", "docs: x", "chore(deps): bump", "refactor: fix"])
def test_other_titles_are_not_checked(title: str) -> None:
    assert mod.check(title, "no section at all", CHANGED, _exists) == []


@pytest.mark.parametrize("title", ["fix: x", "fix(chat): x", "fix!: x", "Fix: x", "revert: x"])
def test_fix_and_revert_titles_are_checked(title: str) -> None:
    assert mod.check(title, "no section at all", CHANGED, _exists)


# -------------------------------------------------------------- red path


def test_missing_heading_is_one_finding_that_shows_the_shape() -> None:
    (finding,) = mod.check("fix: x", "## Tests\n\nx\n", CHANGED, _exists)
    assert "## Sibling sweep" in finding and "Siblings: none" in finding


def test_template_help_text_in_a_comment_does_not_count() -> None:
    body = TEMPLATE.read_text(encoding="utf-8")
    problems = mod.check("fix: x", body, CHANGED, _exists)
    assert problems, "the unfilled template must not satisfy the check"
    assert "no 'Sibling:' line" in problems[0]


def test_a_fenced_example_does_not_count() -> None:
    body = "## Sibling sweep\n\n```\nSiblings: none -- single call site\n```\n"
    assert any("no 'Sibling:' line" in p for p in mod.check("fix: x", body, CHANGED, _exists))


def test_a_shorter_fence_inside_a_longer_one_does_not_close_it() -> None:
    body = "````markdown\n```\n## Sibling sweep\n\nSiblings: none -- single call site\n```\n````\n"
    assert any("must carry" in p for p in mod.check("fix: x", body, CHANGED, _exists))
    # a closing fence of the same length ends the block; one with text after it does not
    assert mod.strip_hidden("```\nhidden\n```\nvisible") == "visible"
    assert mod.strip_hidden("```\nhidden\n``` not a close\nstill hidden\n```\nvisible") == "visible"
    assert mod.strip_hidden("~~~\nhidden\n```\nstill hidden\n~~~\nvisible") == "visible"
    assert mod.strip_hidden("```\nhidden\n`````\nvisible") == "visible"


def test_a_heading_inside_a_comment_does_not_count() -> None:
    body = "<!--\n## Sibling sweep\nSiblings: none -- single call site\n-->\n"
    assert any("must carry" in p for p in mod.check("fix: x", body, CHANGED, _exists))


@pytest.mark.parametrize(
    "section, fragment",
    [
        ("Siblings: none", "needs a reason"),
        ("Siblings: none -- n/a", "needs a reason"),
        ("Sibling: src/b.py -- not affected -- ok", "needs a reason"),
        ("Sibling: src/b.py -- not affected", "after '--'"),
        ("Sibling: src/b.py -- fine", "after '--'"),
        ("Sibling: src/b.py -- tracked in #abc", "after '--'"),
        ("Sibling: src/b.py", "does not parse"),
        ("Sibling: src b.py -- changed here", "does not parse"),
        ("Siblings: src/b.py", "does not parse"),
        ("Sibling: src/zzz.py -- tracked in #4", "not in the head tree or the diff"),
        ("Sibling: src/b.py -- changed here", "not in the diff"),
        ("Sibling: <path> -- changed here", "not in the head tree or the diff"),
        ("Sibling: /etc/passwd -- tracked in #4", "repo-relative"),
        ("Sibling: ../x.py -- tracked in #4", "repo-relative"),
        ("Sibling: src/b.py -- tracked in #4\nSibling: src/b.py -- tracked in #5", "listed twice"),
        ("Siblings: none -- single call site\nSibling: src/b.py -- tracked in #4", "contradicts"),
        ("Swept: git grep foo\nsome prose only", "no 'Sibling:' line"),
        ("", "no 'Sibling:' line"),
    ],
)
def test_malformed_sections_are_reported(section: str, fragment: str) -> None:
    problems = _check(section)
    assert any(fragment in p for p in problems), problems


def test_more_than_forty_lines_is_too_broad() -> None:
    changed = {f"f{i}.py" for i in range(41)}
    lines = "\n".join(f"Sibling: f{i}.py -- changed here" for i in range(41))
    problems = mod.check("fix: x", _body(lines), changed, _exists)
    assert any("limit 40" in p for p in problems)
    ok = "\n".join(f"Sibling: f{i}.py -- changed here" for i in range(40))
    assert mod.check("fix: x", _body(ok), changed, _exists) == []


def test_the_section_ends_at_the_next_heading_of_its_level() -> None:
    body = "## Sibling sweep\n\nSiblings: none -- single call site\n\n## Tests\n\nSibling: nope\n"
    assert mod.check("fix: x", body, CHANGED, _exists) == []


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
        ("2026-01-01T00:00:00Z", False),
    ],
)
def test_enforcement_applies_to_pull_requests_opened_on_or_after_the_date(
    created: str, expected: bool
) -> None:
    assert mod.enforced(created, enforce_from="2026-11-04") is expected


def test_a_pull_request_with_no_opening_time_counts_as_opened_now() -> None:
    assert mod.enforced(None, enforce_from="2000-01-01") is True
    assert mod.enforced(None, enforce_from="2999-01-01") is False


# ------------------------------------------------------------ end to end


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
    env.update(
        {
            "GIT_TEMPLATE_DIR": "",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
    )
    # Pointing GIT_CONFIG_GLOBAL away drops the suite's global file, so the
    # maintenance entries go on the env this helper built.
    env.update(git_maintenance_off_entries(env))
    return env


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_git_env(),
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A base commit with three files, then a fix commit that edits one and adds one."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    for name in ("a.py", "b.py", "c.py"):
        (repo / "src" / name).write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "a.py").write_text("x = 2\n")
    (repo / "src" / "new.py").write_text("y = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "fix: x")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def _run(repo: tuple[Path, str, str], title: str, body: str, **extra: str):
    path, base, head = repo
    env = _git_env()
    env.update({"PR_TITLE": title, "PR_BODY": body, "MERGE_BASE": base, "HEAD_SHA": head})
    env.update(extra)
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPT)],
        cwd=path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_end_to_end_green_list_passes(repo) -> None:
    body = _body("Sibling: src/a.py:1 -- changed here\nSibling: src/b.py -- tracked in #77")
    rc, out = _run(repo, "fix: x", body)
    assert rc == 0, out
    assert "Sibling sweep section present." in out


def test_end_to_end_a_path_the_diff_adds_counts_as_existing(repo) -> None:
    rc, out = _run(repo, "fix: x", _body("Sibling: src/new.py -- changed here"))
    assert rc == 0, out


def test_end_to_end_missing_section_warns_and_passes_in_warn_mode(repo) -> None:
    rc, out = _run(repo, "fix: x", "## Tests\n\nx\n", GITHUB_ACTIONS="true")
    assert rc == 0
    assert out.startswith("::warning::Sibling sweep: ")
    assert "advisory for now" in out


def test_end_to_end_local_run_prints_plain_lines(repo) -> None:
    rc, out = _run(repo, "fix: x", "## Tests\n\nx\n", GITHUB_ACTIONS="")
    assert rc == 0
    assert "SIBLING SWEEP (warning):" in out and "::warning::" not in out


def test_end_to_end_invented_path_is_reported(repo) -> None:
    rc, out = _run(repo, "fix: x", _body("Sibling: src/ghost.py -- tracked in #1"))
    assert rc == 0
    assert "'src/ghost.py' is not in the head tree or the diff" in out


def test_end_to_end_not_a_fix_is_skipped(repo) -> None:
    rc, out = _run(repo, "feat: x", "## Tests\n\nx\n")
    assert rc == 0
    assert "not required" in out


def test_end_to_end_missing_git_inputs_is_an_environment_error(repo) -> None:
    rc, out = _run(repo, "fix: x", "", MERGE_BASE="", HEAD_SHA="")
    assert rc == 2
    assert "MERGE_BASE and HEAD_SHA are required" in out


def test_enforce_mode_turns_a_finding_into_exit_one(
    repo, monkeypatch, capsys, _git_global_config
) -> None:
    path, base, head = repo
    # main() runs git in this process: a hook's exported location must not retarget it.
    # Only the location variables go; conftest's identity and config pins stay. The
    # global config is the floor's hermetic file, which holds only the maintenance keys.
    for key in [k for k in os.environ if k in _GIT_LOCATION_VARS or k == "GIT_PREFIX"]:
        monkeypatch.delenv(key)
    assert os.environ["GIT_CONFIG_GLOBAL"] == str(_git_global_config)
    monkeypatch.chdir(path)
    monkeypatch.setattr(mod, "ENFORCE_FROM", "2026-01-01")
    for key, value in {
        "PR_TITLE": "fix: x",
        "PR_BODY": "## Tests\n\nx\n",
        "MERGE_BASE": base,
        "HEAD_SHA": head,
        "PR_CREATED_AT": "2026-02-01T00:00:00Z",
        "GITHUB_ACTIONS": "true",
    }.items():
        monkeypatch.setenv(key, value)
    assert mod.main() == 1
    assert capsys.readouterr().out.startswith("::error::Sibling sweep: ")
    # opened before the date: still a warning, never red
    monkeypatch.setenv("PR_CREATED_AT", "2025-12-31T00:00:00Z")
    assert mod.main() == 0
    monkeypatch.setenv("PR_BODY", _body("Siblings: none -- single call site"))
    monkeypatch.setenv("PR_CREATED_AT", "2026-02-01T00:00:00Z")
    assert mod.main() == 0


# ---------------------------------------------------------------- wiring


def _steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["pr-hygiene"]["steps"]


def _step_index(fragment: str) -> int:
    for i, step in enumerate(_steps()):
        if fragment.lower() in str(step.get("name", "")).lower():
            return i
    raise AssertionError(f"no pr-hygiene step named like {fragment!r}")


def test_template_block_sits_directly_before_pattern_harvest() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    headings = re.findall(r"^## .*$", text, re.MULTILINE)
    assert headings.index("## Sibling sweep") + 1 == headings.index("## Pattern harvest")
    block = text.split("## Sibling sweep", 1)[1].split("## Pattern harvest", 1)[0]
    assert "Sibling: <path>[:<line>] -- changed here | tracked in #<n> | not affected" in block
    assert "Siblings: none -- <why" in block
    # help text only: no uncommented line could satisfy the check by itself
    assert not [
        ln for ln in re.sub(r"<!--.*?-->", "", block, flags=re.DOTALL).splitlines() if ln.strip()
    ]


def test_the_section_is_not_a_required_heading() -> None:
    """REQUIRED_SECTIONS is matched on every open PR, outside copies included."""
    assert "Sibling sweep" not in DESC_CHECK.read_text(encoding="utf-8")


def test_the_step_runs_after_the_pattern_harvest_step_and_reads_the_base_script() -> None:
    idx = _step_index("Sibling sweep")
    assert idx > _step_index("Pattern harvest")
    step = _steps()[idx]
    run = step["run"]
    # base ref first; the checkout copy only while the base has none
    assert 'git show "$BASE_TIP:.github/scripts/sibling_list_check.py"' in run
    assert "cp .github/scripts/sibling_list_check.py" in run
    # untrusted text arrives through env, never spliced into the script
    assert "${{" not in run
    for key in ("PR_TITLE", "PR_BODY", "MERGE_BASE", "HEAD_SHA", "PR_CREATED_AT"):
        assert key in step["env"]
    assert "::error::" not in run
    assert "continue-on-error" not in step


def test_the_step_reuses_the_merge_base_the_take_away_step_resolves() -> None:
    step = _steps()[_step_index("Sibling sweep")]
    assert step["env"]["MERGE_BASE"] == "${{ steps.diffbase.outputs.sha }}"
    assert _step_index("Resolve diff base") < _step_index("Sibling sweep")


# ------------------------------------------- the documented forms, one site each
# Reviewers wrote `Sibling: \`src/x.py\` -- ...` (backticks, line ranges, list markers,
# an em dash) and the first parser rejected nearly all of it.


@pytest.mark.parametrize(
    "section, fragment",
    [
        ("Siblings: none.", "needs a reason"),  # trailing period, no reason
        ("Sibling: `src/a.py -- changed here", "does not parse"),  # unmatched backtick
        ("Sibling: src/a.py` -- changed here", "does not parse"),
        ("Sibling: ``src/a.py`` -- changed here", "does not parse"),  # ONE pair only
        ("Sibling: `src/a.py`:12 -- changed here", "does not parse"),  # pair around the path only
        ("Sibling: `` -- changed here", "does not parse"),
        ("Sibling: src/a.py:x -- changed here", "does not parse"),
        ("Sibling: src/a.py:10- -- changed here", "does not parse"),
        ("Sibling: `src/zzz.py:3-4` -- tracked in #4", "not in the head tree or the diff"),
        ("Sibling: `/etc/passwd` -- tracked in #4", "repo-relative"),
        ("Sibling: `../x.py` -- tracked in #4", "repo-relative"),
        (
            "Sibling: `src/b.py` -- tracked in #4\nSibling: src/b.py:3 -- tracked in #5",
            "listed twice",
        ),
        ("Sibling: src/a.py\u2014changed here", "does not parse"),  # an em dash needs spaces
    ],
)
def test_backtick_and_range_edges(section: str, fragment: str) -> None:
    assert any(fragment in p for p in _check(section)), _check(section)


# ------------------------------------------- the body reader: every hiding rule


@pytest.mark.parametrize(
    "section",
    [
        "> Sibling: src/a.py -- changed here",  # a quote is prose, not a claim
        "> Siblings: none -- single call site",
        "Swept: Sibling: src/a.py -- changed here",  # inside a free-text line
    ],
)
def test_quoted_and_swept_lines_are_not_claims(section: str) -> None:
    assert any("no 'Sibling:' line" in p for p in _check(section))


@pytest.mark.parametrize(
    "body",
    [
        "```\n## Sibling sweep\nSiblings: none -- single call site\n```\n",
        "~~~\n## Sibling sweep\nSiblings: none -- single call site\n~~~\n",
        "````\n```\n## Sibling sweep\nSiblings: none -- single call site\n```\n````\n",
        # a tilde fence is not closed by a backtick fence, nor a longer one by a shorter
        "~~~\n```\n## Sibling sweep\nSiblings: none -- single call site\n~~~\n",
        "<!--\n## Sibling sweep\nSiblings: none -- single call site\n-->\n",
        "<!-- ```\n-->\n```\n## Sibling sweep\nSiblings: none -- single call site\n```\n",
        "## Sibling sweep\n\n<!-- Siblings: none -- single call site -->\n",  # no claim left
    ],
)
def test_hidden_text_never_satisfies_the_check(body: str) -> None:
    assert mod.check("fix: x", body, CHANGED, _exists)


def test_a_fence_marker_inside_a_comment_does_not_open_a_fence() -> None:
    body = "<!-- ```python -->\n## Sibling sweep\n\nSiblings: none -- single call site\n"
    assert mod.check("fix: x", body, CHANGED, _exists) == []


def test_a_comment_start_inside_a_fence_does_not_open_a_comment() -> None:
    body = "```\n<!--\n```\n## Sibling sweep\n\nSiblings: none -- single call site\n"
    assert mod.check("fix: x", body, CHANGED, _exists) == []


def test_text_around_an_inline_comment_still_counts() -> None:
    body = "## Sibling sweep\n\nSiblings: none <!-- why --> -- single call site\n"
    assert mod.check("fix: x", body, CHANGED, _exists) == []


@pytest.mark.parametrize("eol", ["\r\n"])
def test_crlf_and_cr_line_ends(eol: str) -> None:
    body = eol.join(["## Sibling sweep", "", "Sibling: `src/a.py` -- changed here", "", "## Tests"])
    assert mod.check("fix: x", body, CHANGED, _exists) == []
    fenced = eol.join(["```", "## Sibling sweep", "Siblings: none -- single call site", "```"])
    assert mod.check("fix: x", fenced, CHANGED, _exists)


@pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\x0b", "\x0c", "\x85"])
def test_unicode_line_separators_do_not_start_a_new_line(sep: str) -> None:
    # Markdown does not break a line at these; str.splitlines() does.
    body = f"## Sibling sweep\n\nSwept: grep{sep}Siblings: none -- single call site\n"
    assert any("no 'Sibling:' line" in p for p in mod.check("fix: x", body, CHANGED, _exists))


def test_a_comment_that_closes_on_its_opening_line_hides_only_that_line() -> None:
    # CommonMark: the HTML block ends on the line holding `-->`, as the template check reads it.
    body = "<!-- x --><!--\n## Sibling sweep\n\nSiblings: none -- single call site\n"
    assert mod.check("fix: x", body, CHANGED, _exists) == []


def test_an_unclosed_fence_or_comment_hides_the_rest() -> None:
    for tail in ("```\n", "<!--\n"):
        body = tail + "## Sibling sweep\n\nSiblings: none -- single call site\n"
        assert any("must carry" in p for p in mod.check("fix: x", body, CHANGED, _exists))


# ----------------------------------- the same reader as the template check
# `pr-description-check.sh` decides whether `## Tests` is present; this check must see
# a section heading exactly as it does. The corpus below is identical in
# test_proof_line_check.py, which holds the other copy of the reader.

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
    out = tmp_path / "out"
    out.write_text("", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PR_BODY": _CI_PREFIX + body,
        "GITHUB_OUTPUT": str(out),
    }
    subprocess.run(["bash", str(DESC_CHECK)], env=env, check=True, capture_output=True)
    annotation = next(
        ln for ln in out.read_text("utf-8").splitlines() if ln.startswith("annotation=")
    )
    return "## Tests" not in annotation


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="runs .github/scripts/pr-description-check.sh, which needs a POSIX bash",
)
@pytest.mark.parametrize("body", _CI_BODIES)
def test_a_heading_is_found_exactly_when_the_template_check_finds_it(
    body: str, tmp_path: Path
) -> None:
    found, _ = mod.split_section(_CI_PREFIX + body, "## tests")
    assert (found is not None) is _ci_sees_tests(body, tmp_path), repr(body)
