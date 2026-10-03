"""Unit tests for .github/scripts/take_away_check.py.

The fixtures under test/fixtures/take_away/ are verbatim slices of two
real take-away diffs that broke unlisted readers (an apps-tree hide and a
`config.agents` row prune) plus their real bodies, so the first property
pinned here is the one the gate is for: both are flagged, and both existing
bodies FAIL. The worked examples are in
docs/system-specs/common/take-away-changes.md.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "take_away_check.py"
FIXTURES = ROOT / "test" / "fixtures" / "take_away"
WORKFLOW = ROOT / ".github" / "workflows" / "code-review.yml"
TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"

READER = "Reader: src/kiro_crew/cron_script.py:run_script -- cron -- test_app_cron_imports_own_src"


def _load():
    spec = importlib.util.spec_from_file_location("take_away_check", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load()


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _diff(path: str, added: list[str] = (), removed: list[str] = ()) -> str:
    head = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", "@@ -1,1 +1,1 @@"]
    return "\n".join(head + [f"-{x}" for x in removed] + [f"+{x}" for x in added]) + "\n"


def _body(compat: str) -> str:
    return f"## Problem / Motivation\n\n**Goal:** x\n\n## Backwards compatibility\n\n{compat}\n\n## Tests\n\nx\n"


class TestRealRegressions:
    def test_13273_is_flagged_as_a_hide(self) -> None:
        hits = mod.detect(_fixture("pr13273.diff"))
        assert any(h.startswith("hide:") and "apps_tree" in h for h in hits), hits

    def test_13273_existing_body_fails(self) -> None:
        ok, msg = mod.check(_fixture("pr13273.diff"), _fixture("pr13273_body.md"))
        assert not ok
        # It HAS the section -- it claimed Compatible -- but lists no reader.
        assert "no well-formed 'Reader:' line" in msg

    def test_12798_is_flagged_as_migration_and_row_delete(self) -> None:
        hits = mod.detect(_fixture("pr12798.diff"))
        assert any(h.startswith("migration:") and "crewmate_prune_migration.py" in h for h in hits)
        assert any(h.startswith("delete:") and "agents[" in h for h in hits), hits

    def test_12798_existing_body_fails(self) -> None:
        ok, _ = mod.check(_fixture("pr12798.diff"), _fixture("pr12798_body.md"))
        assert not ok

    def test_a_reader_line_makes_13273_pass(self) -> None:
        body = _fixture("pr13273_body.md").replace(
            "## Backwards compatibility\n", f"## Backwards compatibility\n\n{READER}\n", 1
        )
        ok, msg = mod.check(_fixture("pr13273.diff"), body)
        assert ok, msg


class TestShapes:
    @pytest.mark.parametrize(
        ("diff", "kind"),
        [
            (_diff("src/kiro_crew/x_migration.py", ["pass"]), "migration:"),
            (_diff("src/kiro_crew/a.py", ["    run(argv, extra_hidden_dirs=(root,))"]), "hide:"),
            (
                _diff(
                    "src/kiro_crew/a.py",
                    ["    run(argv, extra_hidden_dirs=extra_hidden_dirs + (apps,))"],
                ),
                "hide:",
            ),
            (_diff("src/kiro_crew/a.py", ["        del cfg.agents[name]"]), "delete:"),
            (_diff("src/kiro_crew/a.py", ["        config.agents.pop(name)"]), "delete:"),
            (_diff("src/kiro_crew/a.py", [], ["def public_api(x):"]), "remove:"),
            (_diff("src/kiro_crew/a.py", [], ["async def public_api(x):"]), "remove:"),
        ],
    )
    def test_each_shape_is_detected(self, diff: str, kind: str) -> None:
        hits = mod.detect(diff)
        assert any(h.startswith(kind) for h in hits), hits

    @pytest.mark.parametrize(
        "diff",
        [
            # tests are never readers the gate asks about
            _diff("test/test_x_migration.py", ["pass"]),
            _diff("test/test_a.py", ["        del cfg.agents[name]"]),
            # a re-wrapped call is not a new hide
            _diff(
                "src/kiro_crew/a.py",
                ["    run(", "        argv,", "        extra_hidden_dirs=hidden,", "    )"],
                ["    run(argv, extra_hidden_dirs=hidden)"],
            ),
            # a re-signatured def and a private def
            _diff("src/kiro_crew/a.py", ["def public_api(x, y):"], ["def public_api(x):"]),
            _diff("src/kiro_crew/a.py", [], ["def _private(x):"]),
            # an indented def: a method or a docstring example, not a module API
            _diff("src/kiro_crew/a.py", [], ["    def run(ctx):"]),
            # forwarding a mask is not adding one
            _diff("src/kiro_crew/a.py", ["    wrap(argv, extra_hidden_dirs=extra_hidden_dirs,)"]),
            # a comment-only edit to a migration file
            _diff("src/kiro_crew/x_migration.py", ["# reworded"], ["# old wording"]),
            # outside product code: CI tooling naming the shapes in prose
            _diff(
                ".github/scripts/c.py",
                ['    """a new `extra_hidden_dirs=` arg, `extra="forbid"`"""'],
            ),
            _diff("scripts/old_migration.py", ["x = 1"]),
            # a pure addition
            _diff("src/kiro_crew/a.py", ["def new_api(x):", "    return x"]),
        ],
    )
    def test_non_take_away_diffs_are_not_flagged(self, diff: str) -> None:
        assert mod.detect(diff) == []

    def test_a_def_moved_to_another_module_is_a_removal(self) -> None:
        diff = _diff("src/kiro_crew/a.py", [], ["def moved(x):"]) + _diff(
            "src/kiro_crew/b.py", ["def moved(x):"]
        )
        assert any("a.py removes public def moved" in h for h in mod.detect(diff))

    def test_a_same_named_test_helper_does_not_hide_a_removed_api(self) -> None:
        diff = _diff("src/kiro_crew/a.py", [], ["def public_api(x):"]) + _diff(
            "test/test_a.py", ["def public_api(x):"]
        )
        assert any("public_api" in h for h in mod.detect(diff))


class TestBody:
    HIT = _diff("src/kiro_crew/a.py", ["        del cfg.agents[name]"])

    def test_no_hit_passes_with_any_body(self) -> None:
        ok, _ = mod.check(_diff("src/kiro_crew/a.py", ["x = 1"]), "")
        assert ok

    def test_missing_section_fails(self) -> None:
        ok, msg = mod.check(self.HIT, "## Problem / Motivation\n\n**Goal:** x\n")
        assert not ok
        assert "no '## Backwards compatibility' section" in msg

    def test_removes_nothing_does_not_satisfy_a_hit(self) -> None:
        ok, _ = mod.check(self.HIT, _body("Removes nothing: only adds a column"))
        assert not ok

    @pytest.mark.parametrize("entry", ["chat", "cron", "subagent", "app", "crew page", "release"])
    def test_every_entry_point_is_accepted(self, entry: str) -> None:
        line = f"Reader: src/kiro_crew/chat.py:resume -- {entry} -- test_resume_keeps_row"
        ok, msg = mod.check(self.HIT, _body(line))
        assert ok, msg

    def test_a_bulleted_reader_line_is_accepted(self) -> None:
        ok, _ = mod.check(self.HIT, _body(f"- {READER}"))
        assert ok

    @pytest.mark.parametrize(
        "line",
        [
            "Reader: src/kiro_crew/chat.py -- chat -- fine",  # no :symbol
            "Reader: src/kiro_crew/chat.py:resume -- dashboard -- fine",  # unknown entry
            "Reader: src/kiro_crew/chat.py:resume -- chat --",  # no why
            "Reader: src/kiro_crew/chat.py:resume - chat - fine",  # wrong separator
        ],
    )
    def test_malformed_reader_lines_fail_and_are_named(self, line: str) -> None:
        ok, msg = mod.check(self.HIT, _body(line))
        assert not ok
        assert "Malformed:" in msg and line in msg

    def test_reader_lines_in_comments_or_fences_do_not_count(self) -> None:
        compat = f"<!-- {READER} -->\n<!--\n{READER}\n-->\n```\n{READER}\n```"
        ok, _ = mod.check(self.HIT, _body(compat))
        assert not ok

    def test_reader_line_in_another_section_does_not_count(self) -> None:
        body = _body("Compatible: yes") + f"\n## Manual verification\n\n{READER}\n"
        ok, _ = mod.check(self.HIT, body)
        assert not ok

    def test_crlf_body_is_read(self) -> None:
        ok, _ = mod.check(self.HIT, _body(READER).replace("\n", "\r\n"))
        assert ok


class TestCli:
    def _run(self, tmp_path: Path, diff: str, body: str) -> subprocess.CompletedProcess[str]:
        f = tmp_path / "pr.diff"
        f.write_text(diff, encoding="utf-8")
        # The ambient env: a Windows interpreter cannot start without SYSTEMROOT.
        env = {**os.environ, "PR_BODY": body}
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(f)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=tmp_path,
            check=False,
        )

    def test_failure_exits_1_with_one_error_annotation(self, tmp_path: Path) -> None:
        r = self._run(tmp_path, _fixture("pr13273.diff"), _fixture("pr13273_body.md"))
        assert r.returncode == 1
        lines = [x for x in r.stdout.splitlines() if x.startswith("::error::")]
        assert len(lines) == 1 and "Reader: <path>:<symbol>" in lines[0]

    def test_body_is_data_not_code(self, tmp_path: Path) -> None:
        body = _body('$(touch pwned) `touch pwned` "; import os; os.system("touch pwned")')
        r = self._run(tmp_path, _diff("src/kiro_crew/a.py", ["x = 1"]), body)
        assert r.returncode == 0
        assert not (tmp_path / "pwned").exists()


class TestWiring:
    def _steps(self) -> list[dict]:
        wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        return wf["jobs"]["pr-hygiene"]["steps"]

    def test_pr_hygiene_runs_the_script_with_the_body_in_env(self) -> None:
        steps = self._steps()
        (step,) = [s for s in steps if "take_away_check.py" in s.get("run", "")]
        assert step["env"]["PR_BODY"] == "${{ github.event.pull_request.body }}"
        # never interpolated into the shell source
        assert "pull_request.body" not in step["run"]

    def test_the_diff_is_taken_from_the_merge_base(self) -> None:
        steps = self._steps()
        names = [s.get("name", "") for s in steps]
        base = names.index("Resolve diff base (merge-base)")
        check = next(i for i, s in enumerate(steps) if "take_away_check.py" in s.get("run", ""))
        assert base < check
        assert "steps.diffbase.outputs.sha" in str(steps[check])

    def test_template_documents_the_exact_line_formats(self) -> None:
        text = TEMPLATE.read_text(encoding="utf-8")
        assert (
            "Reader: <path>:<symbol> -- <entry: chat|cron|subagent|app|crew page|release>"
            " -- <why it still works | test name>"
        ) in text
        assert "Removes nothing: <why>" in text
        assert tuple(mod.ENTRIES) == ("chat", "cron", "subagent", "app", "crew page", "release")
