"""``diff_signals.py --check-body``: a complete ledger AND a short body are both gates.

Prose is a poor ledger for which areas a diff touches: a long walkthrough
hides a stray file better than a short body does, because the reader trusts the
walkthrough and skips the diff. The script keeps the two jobs apart and gates
both. **Accounting** -- every changed area is named somewhere in the body -- is
exit 20. **Length** -- the ``What changed`` prose over ``WORD_LIMIT`` -- is exit
21: with the ledger guaranteed complete, a cap cannot cut a true fact, only a
restated one. When both breach, 20 is returned and both findings print.

Pure helpers are tested directly; one end-to-end run against a throwaway git
repository pins the exit codes and the printed findings.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from git_maintenance_helpers import git_maintenance_off_entries
from skill_script_helpers import load_skill_script

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

ROOT = Path(__file__).resolve().parent.parent
PREPARE_PR = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "kirocrew-prepare-pr"
SCRIPT = PREPARE_PR / "scripts" / "diff_signals.py"
SKILL = PREPARE_PR / "SKILL.md"

ds = load_skill_script("prepare_pr_diff_signals_under_test", SCRIPT)


# ---------------------------------------------------------------- area_of


@pytest.mark.parametrize(
    "path, area",
    [
        ("src/kiro_crew/dashboard/handlers/x.py", "src/kiro_crew/dashboard"),
        ("src/kiro_crew/security.py", "src/kiro_crew/security.py"),
        ("website/src/components/Chat/a.tsx", "website/src/components"),
        ("website/package.json", "website"),
        (".github/workflows/ci.yml", ".github"),
        ("test/test_x.py", "test"),
        ("AGENTS.md", "AGENTS.md"),
    ],
)
def test_area_of_mirrors_the_pr_scope_unit(path: str, area: str) -> None:
    """Same split as the awk in ``.github/workflows/pr-scope.yml``: three levels
    under the two source roots, the top-level component everywhere else."""
    assert ds.area_of(path) == area


@pytest.mark.skipif(shutil.which("awk") is None, reason="awk not on PATH")
def test_area_of_agrees_with_the_pr_scope_awk_program() -> None:
    """The workflow and the script each spell the area unit; run the workflow's own
    awk on a corpus and require the same answers, so a change to one spelling
    fails here instead of diverging silently."""
    workflow = (ROOT / ".github" / "workflows" / "pr-scope.yml").read_text(encoding="utf-8")
    m = re.search(r"awk -F/ '(.*?)'\s*\|", workflow, re.DOTALL)
    assert m, "pr-scope.yml no longer carries the area awk program"
    program = m.group(1)
    corpus = [
        "src/kiro_crew/dashboard/handlers/x.py",
        "src/kiro_crew/security.py",
        "src/kiro_crew/_vendor/pkg/mod.py",
        "website/src/components/Chat/a.tsx",
        "website/src/main.tsx",
        "website/package.json",
        ".github/workflows/ci.yml",
        "test/test_x.py",
        "AGENTS.md",
        "temp-screenshots/shot.png",
    ]
    proc = subprocess.run(
        ["awk", "-F/", program],
        input="\n".join(corpus) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split("\n")[:-1] == [ds.area_of(p) for p in corpus]


# ------------------------------------------------------- unaccounted_areas


def test_area_named_by_any_of_the_three_forms_is_accounted() -> None:
    paths = [
        "src/kiro_crew/dashboard/handlers/chat.py",
        "website/src/components/Chat/Input.tsx",
        "test/test_chat.py",
    ]
    by_area = "touches src/kiro_crew/dashboard and website/src/components; see test/test_chat.py"
    assert ds.unaccounted_areas(paths, by_area) == []
    by_tail = "handlers/chat.py and Chat/Input.tsx changed, test_chat.py pins it"
    assert ds.unaccounted_areas(paths, by_tail) == []
    by_unique_basename = "chat.py, Input.tsx and test_chat.py"
    assert ds.unaccounted_areas(paths, by_unique_basename) == []


def test_unnamed_area_is_reported_with_its_files() -> None:
    paths = ["src/kiro_crew/dashboard/handlers/chat.py", "src/kiro_crew/security.py"]
    missing = ds.unaccounted_areas(paths, "handlers/chat.py now debounces the send button")
    assert missing == [("src/kiro_crew/security.py", ["src/kiro_crew/security.py"])]


def test_generic_basename_never_satisfies_the_accounting() -> None:
    """``__init__.py`` or ``SKILL.md`` alone says nothing about WHICH one changed."""
    paths = ["src/kiro_crew/apps/__init__.py"]
    assert ds.unaccounted_areas(paths, "__init__.py gained an export") == [
        ("src/kiro_crew/apps", paths)
    ]
    assert ds.unaccounted_areas(paths, "apps/__init__.py gained an export") == []


def test_duplicate_basename_across_areas_needs_a_directory() -> None:
    paths = ["src/kiro_crew/a/config.py", "src/kiro_crew/b/config.py"]
    # Bare name is ambiguous between the two areas: neither is accounted.
    missing = ds.unaccounted_areas(paths, "config.py reads the new key")
    assert [m[0] for m in missing] == ["src/kiro_crew/a", "src/kiro_crew/b"]
    assert ds.unaccounted_areas(paths, "a/config.py and b/config.py read the new key") == []


def test_a_shared_prefix_or_suffix_is_not_a_name() -> None:
    """Substring matching would let an unnamed area ride on a neighbour's name."""
    # Area `src/kiro_crew/chat` is not named by the sibling module `chat_runner.py`.
    paths = ["src/kiro_crew/chat/send.py"]
    assert ds.unaccounted_areas(paths, "src/kiro_crew/chat_runner.py retries") == [
        ("src/kiro_crew/chat", paths)
    ]
    # ...but a deeper path inside the area does name it.
    assert ds.unaccounted_areas(paths, "src/kiro_crew/chat/send.py retries") == []
    # Area `test` is not named by the template heading `## Tests` or by `pytest`.
    paths = ["test/test_send.py"]
    assert ds.unaccounted_areas(paths, "## Tests\n\nrun pytest\n") == [("test", paths)]
    assert ds.unaccounted_areas(paths, "see test/test_send.py") == []
    # A unique basename is not named by a longer name that merely contains it.
    paths = ["src/kiro_crew/chat/send.py"]
    assert ds.unaccounted_areas(paths, "resend.py and send.py.bak") == [
        ("src/kiro_crew/chat", paths)
    ]
    assert ds.unaccounted_areas(paths, "`send.py` now waits") == []
    # A sentence may end right after the name.
    assert ds.unaccounted_areas(paths, "Debounced send.py.") == []


def test_rename_counts_both_sides() -> None:
    ns = "R100\tsrc/kiro_crew/old/x.py\tsrc/kiro_crew/new/x.py\nM\ttest/test_x.py\n"
    paths = ds.changed_paths(ns)
    assert paths == ["src/kiro_crew/old/x.py", "src/kiro_crew/new/x.py", "test/test_x.py"]


# ------------------------------------------------------ what_changed_prose


BODY = """## Problem / Motivation

Broken.

## What changed (motivation → approach → change)

One two three four five.

```mermaid
flowchart LR
  A[one]:::ctx --> B[two]:::added
```

| case | Before | After |
|---|---|---|
| x | 🟥 fails | 🟩 passes |

![Settings page](./evidence/after.png)

<!-- a template comment -->

Six seven eight.

## Tests

Nine ten eleven twelve thirteen fourteen.
"""


def test_prose_excludes_fences_tables_images_and_comments() -> None:
    prose = ds.what_changed_prose(BODY)
    assert prose is not None
    assert ds.word_count(prose) == 8
    assert "flowchart" not in prose
    assert "🟩" not in prose
    assert "evidence" not in prose


def test_prose_is_none_without_the_section() -> None:
    assert ds.what_changed_prose("## Problem\n\nx\n\n## Tests\n\ny\n") is None


# -------------------------------------------------------------- check_body


def test_check_body_over_the_limit_is_twenty_one(capsys) -> None:
    ns = "M\tsrc/kiro_crew/x/y.py\n"
    rc = ds.check_body(BODY + "\nAlso x/y.py.\n", ns, word_limit=3)
    out = capsys.readouterr().out
    assert rc == 21
    assert "TOO LONG: What changed is 8 words of prose (limit 3)" in out
    assert "the diff is the evidence" in out
    # The ledger still passed; only the prose failed.
    assert "every changed area is named in the body" in out


def test_check_body_under_the_limit_is_zero(capsys) -> None:
    ns = "M\tsrc/kiro_crew/x/y.py\n"
    rc = ds.check_body(BODY + "\nAlso x/y.py.\n", ns, word_limit=400)
    out = capsys.readouterr().out
    assert rc == 0
    assert "What changed: 8 words of prose (limit 400)" in out
    assert "TOO LONG" not in out


def test_check_body_both_breaches_print_both_and_return_twenty(capsys) -> None:
    ns = "M\tsrc/kiro_crew/x/y.py\nM\tsrc/kiro_crew/z/w.py\n"
    rc = ds.check_body(BODY + "\nx/y.py changed.\n", ns, word_limit=3)
    out = capsys.readouterr().out
    assert rc == 20
    assert "UNACCOUNTED src/kiro_crew/z" in out
    assert "TOO LONG: What changed is" in out


def test_check_body_missing_area_is_twenty_even_when_prose_is_short(capsys) -> None:
    ns = "M\tsrc/kiro_crew/x/y.py\nM\tsrc/kiro_crew/z/w.py\n"
    rc = ds.check_body(BODY + "\nx/y.py changed.\n", ns, word_limit=400)
    out = capsys.readouterr().out
    assert rc == 20
    assert "UNACCOUNTED src/kiro_crew/z" in out
    assert "    src/kiro_crew/z/w.py" in out
    assert "1 unaccounted area(s)" in out


def test_check_body_without_what_changed_still_runs_accounting(capsys) -> None:
    # check_body alone does not gate a missing section; the template check does
    # (test_end_to_end_body_missing_what_changed_exits_twenty_two).
    rc = ds.check_body("## Problem\n\nnames src/kiro_crew/x\n", "M\tsrc/kiro_crew/x/y.py\n", 400)
    out = capsys.readouterr().out
    assert rc == 0
    assert "length check skipped" in out


def _table_body(rows: int) -> str:
    table = "| file | Before | After |\n|---|:--:|---|\n"
    table += "".join("| x/y.py | 🟥 r{0} | 🟩 r{0} |\n".format(i) for i in range(rows))
    # A fenced table is code, not a table row the reader scans.
    fence = "```\n" + "| fenced | row |\n" * 20 + "```\n"
    return (
        "## What changed\n\nx/y.py sends once.\n\n" + table + "\n" + fence + "\n## Tests\n\n| a |\n"
    )


def test_table_rows_exclude_headers_separators_fences_and_other_sections() -> None:
    assert ds.what_changed_table_rows(_table_body(3)) == 3
    assert ds.what_changed_table_rows(BODY) == 1
    assert ds.what_changed_table_rows("## Problem\n\n| a |\n") is None


# Each block holds 20 table rows and 4 words of prose; none of it may count.
_FENCED_EXAMPLE = "| fenced | row |\n" * 20 + "four words of code\n"


def _indented(text: str, indent: int) -> str:
    return "".join(" " * indent + line + "\n" for line in text.splitlines())


@pytest.mark.parametrize(
    "opener, closer, indent",
    [
        ("~~~", "~~~", 0),
        ("~~~~ text", "~~~~~", 0),
        ("   ```", "  ```", 3),
        ("  ```python", "```", 2),
        ("````", "````", 0),
    ],
    ids=["tilde", "long-tilde", "indented-backtick", "indented-info", "four-backticks"],
)
def test_fence_forms_skip_both_word_and_row_counts(opener: str, closer: str, indent: int) -> None:
    # An indented fence's content sits at the fence column; at column 0 it
    # counts (test_an_indented_fence_with_column_zero_content_is_counted).
    body = (
        "## What changed\n\nx/y.py sends once.\n\n"
        + opener
        + "\n"
        + _indented(_FENCED_EXAMPLE, indent)
        + closer
        + "\n\nDone.\n\n## Tests\n\nt\n"
    )
    assert ds.what_changed_table_rows(body) == 0
    prose = ds.what_changed_prose(body)
    assert prose is not None
    assert ds.word_count(prose) == 4
    assert "code" not in prose


@pytest.mark.parametrize(
    "opener, inner",
    [
        ("~~~", "```"),  # the other character does not close
        ("````", "```"),  # a shorter run does not close
        ("```", "``` x"),  # a closer carries no info string
    ],
)
def test_a_non_closing_line_stays_inside_the_fence(opener: str, inner: str) -> None:
    body = (
        "## What changed\n\nShort.\n\n"
        + opener
        + "\n"
        + inner
        + "\n"
        + _FENCED_EXAMPLE
        + opener
        + "\n"
    )
    assert ds.what_changed_table_rows(body) == 0
    assert ds.word_count(ds.what_changed_prose(body)) == 1


def test_four_space_indent_is_not_a_fence_and_an_unclosed_fence_is_counted() -> None:
    # Four spaces is an indented code line, not a fence opener: the rows count.
    body = "## What changed\n\nShort.\n\n    ```\n| a | b |\n| c | d |\n    ```\n"
    assert ds.what_changed_table_rows(body) == 2
    # An unclosed fence is ambiguous to this scanner, so it fails closed: counted.
    body = "## What changed\n\nShort.\n\n~~~\n" + _FENCED_EXAMPLE + "\n## Tests\n\nt\n"
    assert ds.what_changed_table_rows(body) == 20
    # ``Short.``, the ``~~~`` opener and ``four words of code``; rows are not prose.
    assert ds.word_count(ds.what_changed_prose(body)) == 1 + 1 + 4


# A body far over the limit, each word behind a line this scanner reads as an
# opener that never closes. GitHub renders all of it as prose.
_PROSE_WORDS = "word " * 50 + "\n"


@pytest.mark.parametrize(
    "opener",
    [
        "<details>\n```\n",  # raw HTML: a fence line inside an HTML block is text
        "<div>\n~~~\n",
        "- ```\n  ",  # a list item whose fence never closes, words inside it
        "1. ~~~\n   ",
    ],
    ids=["html-details", "html-div", "list-backtick", "list-tilde"],
)
def test_an_unclosed_fence_counts_as_prose_so_a_long_body_fails(capsys, opener: str) -> None:
    body = "## What changed\n\nx/y.py.\n\n" + opener + _PROSE_WORDS + "\n## Tests\n\nt\n"
    assert ds.word_count(ds.what_changed_prose(body)) > 50
    rc = ds.check_body(body, "M\tsrc/kiro_crew/x/y.py\n", word_limit=40)
    assert rc == 21
    assert "TOO LONG" in capsys.readouterr().out


def test_a_backtick_in_the_info_string_is_not_an_opener() -> None:
    # ```a`b is inline code, not a fence. Read as an opener, the later ``` would
    # close it and hide the rows between.
    body = "## What changed\n\n```a`b\n| r1 |\n| r2 |\n```\n"
    assert ds.what_changed_table_rows(body) == 2


def test_a_deep_list_closer_must_reach_the_item_content_indent() -> None:
    # A four-wide item: its closer sits at four spaces (up to seven); three is
    # less than the content indent, so that line ends the item, unclosed.
    closed = "## What changed\n\n1.  ```\n    | a |\n    ```\n"
    assert ds.what_changed_table_rows(closed) == 0
    closed = "## What changed\n\n1.  ```\n    | a |\n       ```\n"
    assert ds.what_changed_table_rows(closed) == 0
    short = "## What changed\n\n1.  ```\n    | a |\n   ```\n| b |\n"
    assert ds.what_changed_table_rows(short) == 2


def _prose_count(section: str) -> int:
    return ds.word_count(ds.what_changed_prose("## What changed\n\n" + section))


@pytest.mark.parametrize(
    "start, end",
    [
        ("<div>", "</div>"),  # ends at a blank line
        ("- <div>", "  </div>"),  # the same block behind a list marker
        ("<pre>\n", "</pre>"),  # runs past a blank line to its end marker
    ],
    ids=["div", "list-div", "pre-past-blank"],
)
def test_a_closed_fence_pair_inside_html_is_counted(start: str, end: str) -> None:
    # Inside an HTML block a fence line is text, so GitHub shows every word.
    pad = "  " if start.startswith("- ") else ""
    section = start + "\n" + pad + "~~~\n" + pad + _PROSE_WORDS + pad + "~~~\n" + end + "\n"
    assert _prose_count(section) >= 50


def test_a_fence_after_an_html_block_ends_still_strips() -> None:
    # The run ends at the blank line; the fence after it is code again.
    section = "<div>x</div>\n\n~~~\n" + _PROSE_WORDS + "~~~\n"
    assert _prose_count(section) == 1  # ``<div>x</div>``


def test_a_tab_indented_line_stays_inside_its_list_fence() -> None:
    # A tab reaches column 4, past the item's content at 2. Read as one column,
    # it ended the fence early, and the closer opened a block hiding the words.
    section = "- ~~~\n\tcode\n  ~~~\n\n" + _PROSE_WORDS + "\n~~~\nexample\n~~~\n"
    assert _prose_count(section) == 50


def test_a_line_left_of_the_fence_ends_it_and_is_counted() -> None:
    # The fence sits on a list item's continuation line; the words at column 0
    # are after the item, and GitHub shows them as a paragraph.
    section = "- a\n\n  ```\n  code\n\n" + _PROSE_WORDS + "\n```\nx\n```\n"
    assert _prose_count(section) >= 50


def test_an_indented_fence_with_column_zero_content_is_counted() -> None:
    # The one valid form the fail-closed rule overcounts: CommonMark renders
    # this as code, but a line left of the fence column is never stripped.
    section = "   ```\n" + _PROSE_WORDS + "   ```\n"
    assert _prose_count(section) >= 50


def test_a_long_list_marker_line_is_scanned_in_linear_time() -> None:
    # Each ``-   `` unit is a marker plus spaces the next unit's indent can also
    # take; with backtracking prefix groups 16 units took ~10 s with no fence.
    from conftest import assert_rejected_without_backtracking

    def reject(text: str) -> None:
        assert ds._FENCE_OPEN.match(text) is None
        assert ds._HTML_START.match(text) is None

    assert_rejected_without_backtracking(reject, lambda n: "-   " * n + "x")


@pytest.mark.parametrize("fence", ["~~~", "```"])
def test_a_list_item_fence_closes_at_its_own_indent_and_keeps_the_prose_between(
    fence: str,
) -> None:
    # Each item's indented closer ends its own block. Read as a top-level opener
    # instead, the first closer would hide the paragraph up to the second one.
    item = "- " + fence + "\n  | in | code |\n  " + fence + "\n"
    prose = "word " * 30 + "\n"
    body = "## What changed\n\n" + item + "\n" + prose + "\n" + item + "\n## Tests\n\nt\n"
    assert ds.what_changed_table_rows(body) == 0
    assert ds.word_count(ds.what_changed_prose(body)) == 30


def test_list_fences_nest_and_end_with_their_item() -> None:
    # An ordered and a nested item; the closer sits at the item's content indent.
    body = "## What changed\n\n1. ```py\n   | a |\n   ```\n- - ~~~\n    | b |\n    ~~~\n\nOne.\n"
    assert ds.what_changed_table_rows(body) == 0
    assert ds.word_count(ds.what_changed_prose(body)) == 1
    # An unclosed fence in an item ends at the first line indented less than it,
    # and is counted: the opener and its lines stay, then scanning resumes.
    body = "## What changed\n\n- ~~~\n  | a |\n\nTwo words.\n| row |\n~~~\n| hid |\n~~~\n"
    assert ds.what_changed_table_rows(body) == 2  # ``| a |`` and ``| row |``, not ``| hid |``
    assert ds.word_count(ds.what_changed_prose(body)) == 2 + 2  # ``- ~~~`` and the two words


def test_a_blockquote_fence_is_still_counted() -> None:
    # Residual, not changed here: ``> ~~~`` is not read as a fence, so its words count.
    body = "## What changed\n\nShort.\n\n> ~~~\n> | a |\n> ~~~\n"
    assert ds.what_changed_table_rows(body) == 0  # ``> |`` does not start with ``|``
    assert ds.word_count(ds.what_changed_prose(body)) == 9  # every token, ``>`` included


def test_a_heading_inside_a_fence_still_ends_the_section() -> None:
    # Residual, not changed here: the section is cut at the first ``## `` line
    # before fences are stripped, so a heading inside a fence ends it.
    # The prose and rows after the fence are then not counted (today's answer);
    # the opener, left unclosed by the cut, counts as one token.
    body = (
        "## What changed\n\nShort.\n\n```\n## not a heading\n```\n\nLost prose here.\n"
        + "| r |\n" * 9
    )
    assert ds.what_changed_table_rows(body) == 0
    assert ds.word_count(ds.what_changed_prose(body)) == 2


def test_check_body_nine_table_rows_is_twenty_one(capsys) -> None:
    assert ds.TABLE_ROW_LIMIT == 8
    rc = ds.check_body(_table_body(9), "M\tsrc/kiro_crew/x/y.py\n")
    out = capsys.readouterr().out
    assert rc == 21
    assert "TOO MANY ROWS: What changed has 9 table rows (limit 8)" in out


def test_check_body_eight_table_rows_is_zero(capsys) -> None:
    rc = ds.check_body(_table_body(8), "M\tsrc/kiro_crew/x/y.py\n")
    out = capsys.readouterr().out
    assert rc == 0
    assert "TOO MANY ROWS" not in out


def test_row_limit_matches_the_body_picture_ceiling() -> None:
    picture = _flat(SKILL.parent / "references" / "body-picture.md")
    words = {8: "eight"}
    assert "or {} rows, is the ceiling".format(words[ds.TABLE_ROW_LIMIT]) in picture


# --------------------------------------------------------------- end to end


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
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "init.templateDir",
            "GIT_CONFIG_VALUE_0": "",
        }
    )
    # Last, so the entries land after the GIT_CONFIG_COUNT this helper set.
    env.update(git_maintenance_off_entries(env))
    return env


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=_git_env())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """One commit on ``origin/main`` (a synthetic remote ref), one commit on top
    touching two areas. Lives in a subdirectory so a test can create an
    outside-the-repo sibling without leaving ``tmp_path``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "src" / "kiro_crew" / "chat").mkdir(parents=True)
    (repo / "src" / "kiro_crew" / "chat" / "send.py").write_text("x = 1\n")
    (repo / "src" / "kiro_crew" / "security.py").write_text("y = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")
    return repo


def _run(cwd: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "main", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_git_env(),
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_end_to_end_inventory_alone_is_unchanged(repo: Path) -> None:
    rc, out = _run(repo)
    assert rc == 0
    assert "=== Files (name-status) ===" in out
    assert "Body check" not in out


def _body(repo: Path) -> Path:
    return repo / ".git" / ds.BODY_FILENAME


def test_end_to_end_body_missing_an_area_exits_twenty(repo: Path) -> None:
    _body(repo).write_text("## What changed\n\nchat/send.py sends once.\n")
    rc, out = _run(repo, "--check-body")
    assert rc == 20
    assert "UNACCOUNTED src/kiro_crew/security.py" in out


def test_end_to_end_complete_but_long_body_exits_twenty_one(repo: Path) -> None:
    prose = "chat/send.py sends once. security.py allows it. " + "word " * ds.WORD_LIMIT
    _body(repo).write_text("## What changed\n\n" + prose + "\n")
    rc, out = _run(repo, "--check-body")
    assert rc == 21
    assert "TOO LONG: What changed is {} words".format(ds.WORD_LIMIT + 6) in out
    assert "every changed area is named in the body" in out


def test_end_to_end_complete_short_body_exits_zero(repo: Path) -> None:
    _body(repo).write_text("## What changed\n\nchat/send.py sends once. security.py allows it.\n")
    rc, out = _run(repo, "--check-body")
    assert rc == 0
    assert "What changed: 6 words of prose (limit {})".format(ds.WORD_LIMIT) in out


def test_the_body_path_is_fixed_inside_the_git_dir() -> None:
    """One file, computed from git, never an argument -- so there is nothing for a
    caller to point at anything else."""
    assert ds.body_path("/some/repo/.git") == os.path.join("/some/repo/.git", "prepare-pr-body.md")
    assert ds.BODY_FILENAME == "prepare-pr-body.md"


def test_end_to_end_missing_body_is_an_env_error(repo: Path) -> None:
    rc, out = _run(repo, "--check-body")
    assert rc == 2
    assert "cannot read body file" in out
    assert ds.BODY_FILENAME in out


def test_end_to_end_a_file_argument_is_refused(repo: Path) -> None:
    """The body path is not an argument; a stray value is an error, not a target."""
    _body(repo).write_text("## What changed\n\nchat/send.py security.py\n")
    rc, out = _run(repo, "--check-body", str(repo / "other.md"))
    assert rc == 2
    assert "Body check" not in out


# ------------------------------------------------------------- the wiring


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_skill_wires_the_check_at_every_body_write_and_gates_both_checks() -> None:
    # The contract's section 3 and 4 text lives in a reference SKILL.md points to.
    flat = _flat(SKILL) + " " + _flat(PREPARE_PR / "references" / "pr-description-contract.md")
    # Phase 1.5, Phase 2 amend, Phase 3 publish -- each names the flag.
    assert flat.count("diff_signals.py --check-body") >= 3
    assert "<body-file>" not in flat
    # The one body path, named once where the body is written.
    assert "$(git rev-parse --absolute-git-dir)/prepare-pr-body.md" in flat
    # The contract says both checks stop the loop, and why the cap is safe.
    assert "### Two checks, two strengths" in flat
    assert "**exit 20** — stop, fix the body or the diff" in flat
    assert "**exit 21** — stop, compress the prose" in flat
    assert "**WARN**, exit 0" not in flat
    assert "SOFT_WORDS" not in flat
    assert "never pad the prose to hide it" in flat
    # The scripts table advertises both exit codes.
    assert "20 unaccounted area · 21 `What changed` over `WORD_LIMIT`" in flat
    # The limit is a constant, not a knob nobody turns.
    assert "--word-limit" not in flat
    assert "--soft-words" not in flat
    # One bound for section 3: the contract's paragraph rule states the number the
    # script enforces, so the two cannot drift apart.
    assert (
        f"well under {ds.WORD_LIMIT} words of prose; `--check-body` stops past that (exit 21, `WORD_LIMIT`)"
        in flat
    )
    assert f"against the {ds.WORD_LIMIT} of section 3" in flat
    # Phase 3 publishes the same file the check read.
    assert (
        "`<body>` below is the checked file, `$(git rev-parse --absolute-git-dir)/prepare-pr-body.md`"
        in flat
    )


def test_skill_reads_the_register_before_the_first_body_write() -> None:
    """Phase 1 step 5 points at the Age 5 register BEFORE the first --check-body,
    and adds the two rules a recital-shaped body breaks."""
    text = SKILL.read_text(encoding="utf-8")
    step = text.index("5. **Reconcile code and description.**")
    first_check = text.index("diff_signals.py --check-body", step)
    register_pointer = text.index("read *Writing register: Age 5* below", step)
    assert register_pointer < first_check
    flat = _flat(SKILL)
    assert "the diff is the evidence, and the body never restates it" in flat
    assert "One general sentence may cover a whole area" in flat
    assert "Lead with a table when the change has more than one moving part" in flat


def test_skill_has_a_cold_reader_step_after_the_check_and_outside_local_review() -> None:
    """The only readability measure: one tool-less subagent reads What changed alone.
    It lives in the skill, not in local_review.py, which mirrors CI prompts byte-for-byte."""
    text = SKILL.read_text(encoding="utf-8")
    step = text.index("5. **Reconcile code and description.**")
    first_check = text.index("diff_signals.py --check-body", step)
    cold = text.index("**Cold reader.**", step)
    assert first_check < cold
    flat = _flat(SKILL)
    assert "In two sentences, what does this PR change for a user, and why?" in flat
    assert "agent `kirocrew-lite`" in flat
    local_review = (PREPARE_PR / "scripts" / "local_review.py").read_text(encoding="utf-8")
    assert "Cold reader" not in local_review
    assert "two sentences" not in local_review


# ------------------------------------------------------ the template check

TEMPLATE_CHECK = ROOT / ".github" / "scripts" / "pr-description-check.sh"
TEMPLATE_BODY = (
    "## Problem / Motivation\n\n**Goal:** send once.\n\n## Why it matters\n\nx\n\n"
    "## Not a goal\n\nx\n\n## What changed\n\nchat/send.py sends once. security.py "
    "allows it. .github is weakened.\n\n## Tests\n\nx\n"
)


# Same predicate as the repo's other bash-executing suites: on Windows the
# `bash` on PATH may be the WSL launcher, which is not a shell.
needs_bash = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="executes bash; needs a POSIX bash on PATH",
)


@pytest.fixture
def templated_repo(repo: Path) -> Path:
    """``repo`` rebased on a base that carries CI's real template check, with a
    branch commit that weakens its own copy to always pass."""
    _git(repo, "checkout", "-q", "--detach", "origin/main")
    (repo / ".github" / "scripts").mkdir(parents=True)
    shutil.copy(TEMPLATE_CHECK, repo / ".github" / "scripts" / TEMPLATE_CHECK.name)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base with check")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "rebase", "-q", "origin/main")
    weak = 'echo "conclusion=success" >> "$GITHUB_OUTPUT"\n'
    (repo / ".github" / "scripts" / TEMPLATE_CHECK.name).write_text(weak)
    _git(repo, "commit", "-q", "-am", "weaken")
    return repo


@needs_bash
def test_end_to_end_body_missing_a_template_section_exits_twenty_two(templated_repo: Path) -> None:
    """The base ref's script runs, not the branch's weakened copy."""
    _body(templated_repo).write_text(TEMPLATE_BODY.replace("## Not a goal\n", ""))
    rc, out = _run(templated_repo, "--check-body")
    assert rc == 22
    assert "TEMPLATE: 1 required description section is missing" in out
    assert "- `## Not a goal`" in out


@needs_bash
def test_end_to_end_body_missing_what_changed_exits_twenty_two(templated_repo: Path) -> None:
    """A body with no What changed never passes --check-body: the template check stops it."""
    body = TEMPLATE_BODY.replace("## What changed\n\n", "## Notes\n\n")
    _body(templated_repo).write_text(body)
    rc, out = _run(templated_repo, "--check-body")
    assert rc == 22
    assert "length check skipped" in out
    assert "- `## What changed`" in out


@needs_bash
def test_end_to_end_template_shaped_body_exits_zero(templated_repo: Path) -> None:
    _body(templated_repo).write_text(TEMPLATE_BODY)
    rc, out = _run(templated_repo, "--check-body")
    assert rc == 0, out
    assert "PR template: required sections present" in out


def test_end_to_end_base_without_the_template_check_skips_it(repo: Path) -> None:
    _body(repo).write_text("## What changed\n\nchat/send.py sends once. security.py allows it.\n")
    rc, out = _run(repo, "--check-body")
    assert rc == 0
    assert "TEMPLATE" not in out
    assert "PR template" not in out
