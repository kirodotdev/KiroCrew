"""Tests for sweep_check.py, the push-time check of the class sweep file.

The check runs inside ``push_guard.py --require-single-on-base``. It proves that the
mate's own search pattern leaves no hit that no site line accounts for, and it applies
the seam rules (3rd fix: change approach, 5th fix: narrow or defer) from the fix counts
the PR thread records. It never asks a person, and it is advisory while
``SWEEP_ENFORCED`` is False.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import types
from pathlib import Path

import push_guard_helpers
import pytest
from push_guard_helpers import (
    _commit_tree,
    _git,
    _inprocess,
    _land_on_main,
    _load_push_guard,
    _run_push_guard,
)
from skill_script_helpers import load_skill_script

_no_inherited_git_c = push_guard_helpers._no_inherited_git_c
_repo_pair_template = push_guard_helpers._repo_pair_template
repo_pair = push_guard_helpers.repo_pair

SCRIPT = push_guard_helpers.PUSH_GUARD_DIR / "sweep_check.py"
SPAN = "abcdef012345"
CLASS = "a git hook can come from a source other than core.hooksPath"
SEARCH = "git grep -nE 'hooksPath' -- src/kiro_crew"
A = "src/kiro_crew/a.py"
B = "src/kiro_crew/b.py"
REASON = "reads the name only, never runs the command"


def _mod():
    return load_skill_script("sweep_check_under_test", SCRIPT)


@pytest.fixture
def clone(repo_pair):
    """A feature branch that fixes a.py; b.py still names hooksPath; one test is added."""
    clone_dir, _ = repo_pair
    _land_on_main(
        clone_dir,
        {A: "x = 1\nhooksPath = 1\n", B: "def f():\n    return core.hooksPath\n"},
        "feat: base",
    )
    _git(clone_dir, "checkout", "-q", "-b", "feature/x")
    _commit_tree(
        clone_dir,
        {A: "x = 1\nhooks_path = 1\n", "test/test_a.py": "def test_hooks():\n    pass\n"},
        "fix: a",
    )
    return clone_dir


def _entry(
    *,
    span: str = SPAN,
    klass: str = CLASS,
    search: str = SEARCH,
    sites: tuple[str, ...] = (f"- {A}:2  fixed", f"- {B}:2  not-in-class -- {REASON}"),
    branches: tuple[str, ...] = (),
    extra: str = "",
) -> str:
    out = f"## span={span}\nclass: {klass}\nsearch: {search}\n{extra}"
    if sites:
        out += "sites:\n" + "\n".join(sites) + "\n"
    if branches:
        out += "branches:\n" + "\n".join(branches) + "\n"
    return out


def _check(mod, clone_dir, text, monkeypatch, *, enforced=True, counts=None, pushes=None):
    git_dir = _git(clone_dir, "rev-parse", "--absolute-git-dir")
    _write_sweep(git_dir, text)
    monkeypatch.setattr(mod, "SWEEP_ENFORCED", enforced)
    return mod.run(
        "main", git_dir, cwd=clone_dir, prior_counts=lambda: counts, blocked_pushes=lambda: pushes
    )


def _write_sweep(git_dir, text):
    """The sweep file with exactly these bytes (no newline translation on Windows)."""
    Path(git_dir, "prepare-pr-sweep.md").write_bytes(text.encode("utf-8"))


def _write_script(path, marker=None):
    """An executable stand-in that creates `marker` when it runs, or exits 0 without one.

    A batch file on Windows, a shell script elsewhere, so a test can tell
    whether a launcher ran on either platform.
    """
    if sys.platform == "win32":
        action = f'type nul > "{marker}"' if marker else "exit /b 0"
        path.write_bytes(f"@echo off\r\n{action}\r\n".encode("ascii"))
        return
    action = f"touch '{marker}'" if marker else "exit 0"
    path.write_bytes(f"#!/bin/sh\n{action}\n".encode("ascii"))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


# Windows cannot hold ':' or a newline in a file name.
needs_posix_names = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows file names cannot hold ':' or a newline"
)


_FAKE_GH_SOURCE = (
    "import json, os, sys\n"
    "if 'FAKE_GH' not in os.environ:\n"
    "    open(os.environ['FAKE_GH_UNSTUBBED'], 'w').close()\n"
    "    sys.exit(1)\n"
    "data = json.load(open(os.environ['FAKE_GH']))\n"
    "a = sys.argv[1:]\n"
    "if a[:2] == ['pr', 'view']:\n"
    "    print(json.dumps(data['pr']))\n"
    "elif a[0] == 'api' and '/comments' in a[1]:\n"
    "    print(json.dumps(data['comments']))\n"
    "elif a[0] == 'api' and '/collaborators/' in a[1]:\n"
    "    print(json.dumps({'permission': 'write'}))\n"
    "elif a[0] == 'api' and '/timeline' in a[1]:\n"
    "    print(json.dumps(data.get('timeline', [])))\n"
    "elif a[0] == 'api' and '/check-runs' in a[1]:\n"
    "    rows = data.get('checks', {}).get(a[1].split('/commits/')[1].split('/')[0], [])\n"
    "    print(json.dumps({'total_count': len(rows), 'check_runs': rows}))\n"
    "else:\n"
    "    sys.exit(1)\n"
)


def _write_launcher(path, script):
    """A `gh` that runs `script` with this Python: a .cmd on Windows, a shell script elsewhere."""
    if sys.platform == "win32":
        body = f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n'
        path.with_suffix(".cmd").write_bytes(body.encode("utf-8"))
        return
    path.write_bytes(f"#!/bin/sh\nexec '{sys.executable}' '{script}' \"$@\"\n".encode("utf-8"))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture(autouse=True)
def _hermetic_gh(tmp_path, _floor_monkeypatch):
    """No test reaches the real gh: a fake one leads PATH and the GitHub env is cleared.

    Patches go through the isolation floor's own MonkeyPatch, so a test that calls
    `monkeypatch.undo()` cannot lift them.

    A test that wants a PR thread calls `_fake_gh`. A test that reaches gh without one
    fails here, so a new unstubbed call cannot slip in through a subprocess.
    """
    for name in ("GH_REPO", "GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", "GH_ENTERPRISE_TOKEN"):
        _floor_monkeypatch.delenv(name, raising=False)
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    script = bindir / "fake_gh.py"
    script.write_bytes(_FAKE_GH_SOURCE.encode("ascii"))
    _write_launcher(bindir / "gh", script)
    unstubbed = tmp_path / "gh-unstubbed"
    _floor_monkeypatch.setenv("FAKE_GH_UNSTUBBED", str(unstubbed))
    _floor_monkeypatch.setenv("FAKE_GH_SCRIPT", str(script))
    _floor_monkeypatch.delenv("FAKE_GH", raising=False)
    _floor_monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    yield
    assert not unstubbed.exists(), "a test reached gh without _fake_gh"


def _refused(mod, clone_dir, text, monkeypatch, capsys, **kw):
    rc = _check(mod, clone_dir, text, monkeypatch, **kw)
    return rc, capsys.readouterr().err


class TestGreenPath:
    def test_an_accounted_sweep_passes(self, clone, monkeypatch, capsys):
        mod = _mod()
        rc = _check(mod, clone, _entry(), monkeypatch, counts={})
        assert rc == 0
        assert "1 sweep entry checked, no findings" in capsys.readouterr().out

    def test_no_sweep_file_passes_silently(self, clone, capsys):
        mod = _mod()
        git_dir = _git(clone, "rev-parse", "--absolute-git-dir")
        rc = mod.run("main", git_dir, cwd=clone, prior_counts=lambda: {}, blocked_pushes=lambda: 2)
        assert rc == 0
        assert capsys.readouterr() == ("", "")
        row = json.loads(Path(git_dir, "prepare-pr-metrics.jsonl").read_text().splitlines()[-1])
        assert row["outcome"] == "no-file" and row["entries"] == 0 and row["findings"] == 0

    def test_covered_by_a_test_in_the_diff_passes(self, clone, monkeypatch):
        sites = (f"- {A}:2  fixed", f"- {B}:2  covered-by test/test_a.py::test_hooks")
        assert _check(_mod(), clone, _entry(sites=sites), monkeypatch, counts={}) == 0

    def test_a_structural_class_with_two_branches_passes(self, clone, monkeypatch):
        text = _entry(
            search="none -- ordering across three writers, no common token",
            sites=(),
            branches=("- first PUT fails  fixed", "- slot mid-cycle  covered-by test/test_a.py"),
        )
        assert _check(_mod(), clone, text, monkeypatch, counts={}) == 0

    def test_a_metrics_line_is_appended(self, clone, monkeypatch):
        mod = _mod()
        _check(mod, clone, _entry(), monkeypatch, counts={})
        git_dir = _git(clone, "rev-parse", "--absolute-git-dir")
        row = json.loads(Path(git_dir, "prepare-pr-metrics.jsonl").read_text().splitlines()[-1])
        assert row["step"] == "sweep_check" and row["outcome"] == "ok" and row["entries"] == 1


class TestRefusals:
    def test_a_hit_no_site_accounts_for_is_refused_with_the_hit_list(
        self, clone, monkeypatch, capsys
    ):
        rc, err = _refused(
            _mod(), clone, _entry(sites=(f"- {A}:2  fixed",)), monkeypatch, capsys, counts={}
        )
        assert rc == 40
        assert "REFUSED" in err and "no site line accounts for" in err and f"{B}:2" in err

    def test_a_vacuous_pattern_is_refused(self, clone, monkeypatch, capsys):
        text = _entry(search="git grep -nE 'neverAppears' -- src/kiro_crew")
        rc, err = _refused(_mod(), clone, text, monkeypatch, capsys, counts={})
        assert rc == 40 and "matches nothing in the tree before the fix" in err

    def test_more_than_25_hits_is_too_broad(self, clone, monkeypatch, capsys):
        _commit_tree(clone, {B: "".join("hooksPath = %d\n" % i for i in range(30))}, "fix: b")
        rc, err = _refused(_mod(), clone, _entry(), monkeypatch, capsys, counts={})
        assert rc == 40 and "30 hits (limit 25): too broad" in err

    def test_covered_by_must_name_a_test_in_the_diff(self, clone, monkeypatch, capsys):
        for test_id, why in (
            ("test/test_other.py::test_x", "is not in the diff"),
            ("test/test_a.py::test_missing", "has no test_missing"),
        ):
            sites = (f"- {A}:2  fixed", f"- {B}:2  covered-by {test_id}")
            rc, err = _refused(_mod(), clone, _entry(sites=sites), monkeypatch, capsys, counts={})
            assert rc == 40 and why in err

    @pytest.mark.parametrize("test_id", ["test/test_old.py", "test/test_old.py::test_old"])
    def test_a_deleted_test_is_neither_coverage_nor_a_deferral_pin(
        self, clone, monkeypatch, capsys, test_id
    ):
        # main gains test_old.py; the feature branch lacks it, so it is in the diff but not in HEAD
        _land_on_main(clone, {"test/test_old.py": "def test_old():\n    pass\n"}, "test: old")
        _git(clone, "checkout", "-q", "feature/x")
        sites = (f"- {A}:2  fixed", f"- {B}:2  covered-by {test_id}")
        rc, err = _refused(_mod(), clone, _entry(sites=sites), monkeypatch, capsys, counts={})
        assert rc == 40 and "does not exist in HEAD (a deleted test covers nothing)" in err
        extra = f"resolution: deferred #12 pinned-by {test_id}\n"
        rc, err = _refused(
            _mod(), clone, _entry(extra=extra), monkeypatch, capsys, counts={SPAN: 4}
        )
        assert rc == 40 and "does not exist in HEAD" in err

    def test_fixed_must_name_a_path_the_diff_touches(self, clone, monkeypatch, capsys):
        sites = (f"- {A}:2  fixed", f"- {B}:2  fixed")
        rc, err = _refused(_mod(), clone, _entry(sites=sites), monkeypatch, capsys, counts={})
        assert rc == 40 and "`fixed` but the diff does not touch" in err

    def test_a_short_not_in_class_reason_is_refused(self, clone, monkeypatch, capsys):
        sites = (f"- {A}:2  fixed", f"- {B}:2  not-in-class -- harmless")
        rc, err = _refused(_mod(), clone, _entry(sites=sites), monkeypatch, capsys, counts={})
        assert rc == 40 and "needs a reason of 20+ characters" in err

    def test_search_none_needs_two_branch_rows(self, clone, monkeypatch, capsys):
        text = _entry(
            search="none -- ordering, no common token", sites=(), branches=("- one  fixed",)
        )
        rc, err = _refused(_mod(), clone, text, monkeypatch, capsys, counts={})
        assert rc == 40 and "two or more rows" in err

    @pytest.mark.parametrize(
        "search",
        [
            "git grep -nE '-oops' -- src/kiro_crew",
            "git grep -e hooksPath -- src/kiro_crew",
            "git grep --open-files-in-pager=touch hooksPath",
            "git grep --no-index hooksPath",
            "git grep -nE hooksPath -- :(top)src",
            "git grep hooksPath extra",
            "grep -rn hooksPath .",
            "git grep 'unterminated",
            "",
        ],
    )
    def test_an_unsafe_or_malformed_search_line_is_refused(
        self, clone, monkeypatch, capsys, search
    ):
        rc, err = _refused(_mod(), clone, _entry(search=search), monkeypatch, capsys, counts={})
        assert rc == 40 and "span=" + SPAN in err

    def test_the_pattern_reaches_git_as_data_not_through_a_shell(self, clone, monkeypatch, capsys):
        marker = Path(clone, "pwned")
        text = _entry(search=f"git grep -nE 'hooksPath;touch {marker}' -- src/kiro_crew")
        rc, _ = _refused(_mod(), clone, text, monkeypatch, capsys, counts={})
        assert rc == 40 and not marker.exists()

    def test_a_malformed_row_and_a_bad_span_heading_are_findings(self, clone, monkeypatch, capsys):
        text = _entry(sites=(f"- {A}:2  maybe",)) + "\n## span=xyz\nclass: y\n"
        rc, err = _refused(_mod(), clone, text, monkeypatch, capsys, counts={})
        assert rc == 40
        assert "row does not end in fixed" in err and "a span heading is exactly" in err

    def test_a_missing_class_sentence_is_refused(self, clone, monkeypatch, capsys):
        rc, err = _refused(_mod(), clone, _entry(klass=""), monkeypatch, capsys, counts={})
        assert rc == 40 and "missing `class:` sentence" in err


class TestWarnPeriod:
    def test_the_same_findings_print_and_the_exit_is_zero(self, clone, monkeypatch, capsys):
        mod = _mod()
        assert mod.SWEEP_ENFORCED is False, "the check ships in its warn period"
        text = _entry(sites=(f"- {A}:2  fixed",))
        rc, err = _refused(mod, clone, text, monkeypatch, capsys, enforced=False, counts={})
        assert rc == 0
        assert "WARNING (warn period, not refused)" in err and f"{B}:2" in err
        git_dir = _git(clone, "rev-parse", "--absolute-git-dir")
        row = json.loads(Path(git_dir, "prepare-pr-metrics.jsonl").read_text().splitlines()[-1])
        assert row["outcome"] == "warned" and row["findings"] == 1 and row["enforced"] is False


class TestSeamRules:
    """The 3rd fix of one span changes approach; the 5th narrows or defers. No person."""

    def test_the_second_fix_needs_nothing_extra(self, clone, monkeypatch):
        assert _check(_mod(), clone, _entry(), monkeypatch, counts={SPAN: 1}) == 0

    def test_the_third_fix_needs_a_resolution_or_a_branches_table(self, clone, monkeypatch, capsys):
        rc, err = _refused(_mod(), clone, _entry(), monkeypatch, capsys, counts={SPAN: 2})
        assert rc == 40 and "this is the 3rd fix of one span" in err
        table = (
            "- final drain, first PUT fails  fixed",
            "- slot mid-cycle  covered-by test/test_a.py",
        )
        assert _check(_mod(), clone, _entry(branches=table), monkeypatch, counts={SPAN: 2}) == 0
        extra = "resolution: narrowed -- redo the hook lookup without the include path\n"
        assert _check(_mod(), clone, _entry(extra=extra), monkeypatch, counts={SPAN: 2}) == 0

    def test_the_fifth_fix_needs_a_resolution_and_a_table_is_not_enough(
        self, clone, monkeypatch, capsys
    ):
        table = (
            "- final drain, first PUT fails  fixed",
            "- slot mid-cycle  covered-by test/test_a.py",
        )
        rc, err = _refused(
            _mod(), clone, _entry(branches=table), monkeypatch, capsys, counts={SPAN: 4}
        )
        assert rc == 40 and "this is the 5th fix of one span: narrow the scope or defer" in err

    @pytest.mark.parametrize(
        "resolution",
        [
            "removed -- the second writer is gone and the hook list has one source",
            "narrowed -- only the hooksPath key is read now, includeIf is out of scope",
            "deferred #4711 pinned-by test/test_a.py::test_hooks",
        ],
    )
    def test_the_fifth_fix_passes_with_a_valid_resolution(self, clone, monkeypatch, resolution):
        extra = f"resolution: {resolution}\n"
        assert _check(_mod(), clone, _entry(extra=extra), monkeypatch, counts={SPAN: 4}) == 0

    @pytest.mark.parametrize(
        ("resolution", "why"),
        [
            ("narrowed -- short", "needs a reason of 20+ characters"),
            ("deferred later", "resolution must read"),
            ("deferred #12 pinned-by test/test_gone.py::test_x", "is not in the diff"),
            ("fixed again", "resolution must read"),
        ],
    )
    def test_an_invalid_resolution_is_refused_at_any_count(
        self, clone, monkeypatch, capsys, resolution, why
    ):
        extra = f"resolution: {resolution}\n"
        rc, err = _refused(_mod(), clone, _entry(extra=extra), monkeypatch, capsys, counts={})
        assert rc == 40 and why in err

    @pytest.mark.parametrize("risk", ["security", "data-loss", "corruption"])
    def test_a_sensitive_class_is_never_deferred(self, clone, monkeypatch, capsys, risk):
        extra = f"risk: {risk}\nresolution: deferred #4711 pinned-by test/test_a.py::test_hooks\n"
        rc, err = _refused(
            _mod(), clone, _entry(extra=extra), monkeypatch, capsys, counts={SPAN: 4}
        )
        assert rc == 40 and f"a {risk} class is never deferred" in err
        narrowed = f"risk: {risk}\nresolution: narrowed -- only the hooksPath key is read now\n"
        assert _check(_mod(), clone, _entry(extra=narrowed), monkeypatch, counts={SPAN: 4}) == 0

    def test_unreadable_rounds_skip_the_seam_rules_and_say_so(self, clone, monkeypatch, capsys):
        rc, err = _refused(_mod(), clone, _entry(), monkeypatch, capsys, counts=None)
        assert rc == 0
        assert "seam rules skipped" in err

    def test_counts_come_from_the_pr_thread_by_default(self, clone, monkeypatch):
        mod = _mod()
        seen: list = []
        monkeypatch.setattr(mod, "_prior_fix_counts", lambda cwd: seen.append(cwd) or {SPAN: 4})
        monkeypatch.setattr(mod, "_blocked_pushes", lambda cwd: None)
        git_dir = _git(clone, "rev-parse", "--absolute-git-dir")
        _write_sweep(git_dir, _entry())
        monkeypatch.setattr(mod, "SWEEP_ENFORCED", True)
        assert mod.run("main", git_dir, cwd=clone) == 40
        assert [os.path.realpath(c) for c in seen] == [os.path.realpath(clone)]


class TestPriorFixCounts:
    def test_a_missing_gh_or_pr_means_unknown_not_zero(self, tmp_path, monkeypatch):
        mod = _mod()
        monkeypatch.setenv("PATH", str(tmp_path))
        assert mod._prior_fix_counts(str(tmp_path)) is None

    def test_a_fix_counts_once_per_distinct_head_and_only_fixes_count(self):
        mod = _mod()
        heads = ["1" * 40, "2" * 40, "3" * 40]

        def rec(cid, head, span):
            return {"comment_id": cid, "head": head, "spans": [span], "malformed": False}

        records = [
            rec(1, heads[0], SPAN),
            rec(2, heads[0], SPAN),
            rec(3, heads[1], SPAN),
            rec(4, heads[2], SPAN),
            rec(5, heads[0], "bbbbbbbbbbbb"),
            rec(6, heads[1], "bbbbbbbbbbbb"),
            rec(7, heads[0], "cccccccccccc"),
        ]
        bodies = {
            1: "**fixed** -- x\nclass: a hook can come from another source\n",
            2: "**fixed** -- again\nclass: a hook can come from another source\n",
            3: "**fixed** -- x\nclass: a hook can come from another source\n",
            4: "**rebutted** -- not a fix\n",
            5: "**fixed** -- x\nclass: the export drops the trailing newline\n",
            6: "**fixed** -- x\nclass: retry loop sleeps while holding the lock\n",
            7: "> fixed in an earlier head, quoted\nclass: a thing\n",
        }
        counts = mod.fix_counts_from(records, bodies)
        assert counts == {SPAN: 2, "bbbbbbbbbbbb": 2}, "heads are counted; class lines are not read"
        assert mod.fix_counts_from(records, bodies, skip_head=heads[0])[SPAN] == 1

    def test_class_lines_never_change_the_count(self):
        mod = _mod()
        classes = ["a hook can come from another source"] + ["the export drops the newline"] * 3
        records = [
            {"comment_id": i, "head": str(i + 1) * 40, "spans": [SPAN], "malformed": False}
            for i in range(4)
        ]
        bodies = {i: f"**fixed** -- x\nclass: {c}\n" for i, c in enumerate(classes)}
        assert mod.fix_counts_from(records, bodies) == {SPAN: 4}
        assert "difflib" not in SCRIPT.read_text(encoding="utf-8")

    def test_every_span_of_a_fixing_record_is_kept(self):
        mod = _mod()
        records = [
            {
                "comment_id": 1,
                "head": "1" * 40,
                "spans": [SPAN, "bbbbbbbbbbbb"],
                "malformed": False,
            },
            {
                "comment_id": 2,
                "head": "2" * 40,
                "spans": ["bbbbbbbbbbbb", SPAN],
                "malformed": False,
            },
            {"comment_id": 3, "head": "3" * 40, "spans": [SPAN], "malformed": True},
        ]
        bodies = {1: "**fixed** -- x\n", 2: "**fixed** -- y\n", 3: "**fixed** -- z\n"}
        assert mod.fix_counts_from(records, bodies) == {SPAN: 2, "bbbbbbbbbbbb": 2}

    def test_a_rebuttal_that_quotes_a_fixed_bullet_is_not_a_fix(self):
        mod = _mod()
        quote = "> - **fixed** in head 1\n> Outcome: fixed in abc123\n"
        records = [
            {"comment_id": i, "head": str(i + 1) * 40, "spans": [SPAN], "malformed": False}
            for i in range(4)
        ]
        bodies = {i: f"**rebutted** -- span={SPAN}\n{quote}" for i in range(4)}
        assert mod.fix_counts_from(records, bodies) == {}

    def test_a_bold_title_with_no_outcome_line_counts_nothing(self):
        mod = _mod()
        records = [
            {"comment_id": i, "head": str(i + 1) * 40, "spans": [SPAN], "malformed": False}
            for i in range(4)
        ]
        bodies = {
            i: f"**Fixed point iteration is slow** span={SPAN}\n> rationale\n" for i in range(4)
        }
        assert mod.fix_counts_from(records, bodies) == {}


class TestPushGuardWiring:
    def test_a_refusal_from_the_check_refuses_the_push(self, clone, monkeypatch):
        rc, out, _ = _inprocess(
            monkeypatch,
            clone,
            extra_args=["--require-single-on-base"],
            overrides={"_sweep_check": lambda base, git_dir: 40},
        )
        assert rc == 40 and "SAFE TO PUSH" not in out

    def test_a_clean_check_leaves_the_push_safe(self, clone, monkeypatch):
        rc, out, _ = _inprocess(
            monkeypatch,
            clone,
            extra_args=["--require-single-on-base"],
            overrides={"_sweep_check": lambda base, git_dir: 0},
        )
        assert rc == 0 and "SAFE TO PUSH (single commit on base)" in out

    def test_the_real_check_is_advisory_in_the_warn_period(self, clone, tmp_path, monkeypatch):
        git_dir = _git(clone, "rev-parse", "--absolute-git-dir")
        _write_sweep(git_dir, _entry(sites=(f"- {A}:2  fixed",)))
        _fake_gh(tmp_path, monkeypatch, heads=[])
        rc, out, err = _run_push_guard(clone, ["--require-single-on-base"])
        assert rc == 0, err
        assert "SAFE TO PUSH" in out
        assert "sweep_check WARNING (warn period, not refused)" in err

    def test_glue_returns_the_refusal_once_the_check_is_enforced(self, clone, monkeypatch):
        pg = _load_push_guard()
        loader = pg._NoBytecodeSourceLoader
        real = loader.exec_module

        def enforced(self, module):
            real(self, module)
            module.SWEEP_ENFORCED = True
            module._prior_fix_counts = lambda cwd: {}
            module._blocked_pushes = lambda cwd: None

        monkeypatch.setattr(loader, "exec_module", enforced)
        monkeypatch.chdir(clone)
        git_dir = _git(clone, "rev-parse", "--absolute-git-dir")
        _write_sweep(git_dir, _entry(sites=(f"- {A}:2  fixed",)))
        assert pg._sweep_check("main", git_dir) == pg.EXIT_REFUSED
        _write_sweep(git_dir, _entry())
        assert pg._sweep_check("main", git_dir) == 0

    def test_a_contract_module_without_the_fence_reader_warns_and_does_not_block(
        self, clone, monkeypatch, capsys
    ):
        pg = _load_push_guard()
        loader = pg._NoBytecodeSourceLoader
        real = loader.exec_module

        def bare_contract(self, module):
            real(self, module)
            module._SIBLINGS["_review_contract.py"] = types.SimpleNamespace()

        monkeypatch.setattr(loader, "exec_module", bare_contract)
        monkeypatch.chdir(clone)
        git_dir = _git(clone, "rev-parse", "--absolute-git-dir")
        _write_sweep(git_dir, _entry())
        assert pg._sweep_check("main", git_dir) == 0
        assert "could not run" in capsys.readouterr().err

    def test_a_check_that_cannot_load_warns_and_does_not_block(self, clone, monkeypatch, capsys):
        pg = _load_push_guard()

        def boom(self, module):
            raise ImportError("broken copy")

        monkeypatch.setattr(pg._NoBytecodeSourceLoader, "exec_module", boom)
        assert pg._sweep_check("main", _git(clone, "rev-parse", "--absolute-git-dir")) == 0
        assert "could not run (broken copy); not blocking the push" in capsys.readouterr().err


def test_the_script_and_the_skill_state_the_same_limits():
    mod = _mod()
    doc = mod.__doc__ or ""
    assert mod.MAX_HITS == 25 and mod.MIN_REASON == 20
    assert "more than 25 hits" in doc and "under 20 characters" in doc
    assert "exit" in doc.lower() and "No rule asks a person" in doc


def test_agents_md_and_the_skill_point_at_the_sweep():
    root = push_guard_helpers.REPO_ROOT
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "### A review finding is a class, not a line" in agents
    assert "On the fifth fix only narrowing or deferral remain." in agents
    skill = (push_guard_helpers.PUSH_GUARD_DIR.parent / "SKILL.md").read_text(encoding="utf-8")
    assert "`prepare-pr-sweep.md`" in skill
    assert len(skill.encode("utf-8")) <= 56000


def _fake_gh(
    tmp_path, monkeypatch, *, heads, head_now="9" * 40, klass=CLASS, timeline=(), checks=None
):
    """Point the fake gh (see `_hermetic_gh`) at one PR, its dispositions and a writer.

    `timeline` is the PR's timeline events and `checks` {head sha: [check run, ...]}.
    """
    comments = [
        {
            "id": 100 + i,
            "user": {"login": "writer"},
            "body": (
                f"<!-- ai-review-disposition target=gpt head={h} -->\n"
                f"**fixed** -- span={SPAN}\nclass: {klass}\n> rationale\n"
            ),
        }
        for i, h in enumerate(heads)
    ]
    pr = {"number": 7, "url": "https://github.com/o/r/pull/7", "headRefOid": head_now}
    data = tmp_path / "gh.json"
    data.write_text(
        json.dumps(
            {"pr": pr, "comments": comments, "timeline": list(timeline), "checks": checks or {}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_GH", str(data))


class TestSeamPathReadsThePrThread:
    """The 3rd/5th-fix rules end to end: no stub of the fix counts, a gh that serves a thread."""

    def _run(self, mod, clone_dir, text, monkeypatch):
        git_dir = _git(clone_dir, "rev-parse", "--absolute-git-dir")
        _write_sweep(git_dir, text)
        monkeypatch.setattr(mod, "SWEEP_ENFORCED", True)
        # Only the gh executable is swapped (a script run by this Python, so it
        # works on every platform); the fix counting under test is the real code.
        guard = mod._sibling("push_guard.py")
        real = guard.tool_argv

        def argv(args):
            if args[0] == "gh":
                return [sys.executable, os.environ["FAKE_GH_SCRIPT"], *args[1:]]
            return real(args)

        monkeypatch.setattr(guard, "tool_argv", argv)
        return mod.run("main", git_dir, cwd=clone_dir)

    def test_four_fixed_heads_make_the_next_fix_its_fifth(
        self, clone, tmp_path, monkeypatch, capsys
    ):
        _fake_gh(tmp_path, monkeypatch, heads=["1" * 40, "2" * 40, "3" * 40, "4" * 40])
        mod = _mod()
        assert self._run(mod, clone, _entry(), monkeypatch) == 40
        assert "this is the 5th fix of one span" in capsys.readouterr().err
        extra = "resolution: narrowed -- only the hooksPath key is read now\n"
        assert self._run(mod, clone, _entry(extra=extra), monkeypatch) == 0

    def test_two_fixed_heads_make_the_next_fix_its_third(
        self, clone, tmp_path, monkeypatch, capsys
    ):
        _fake_gh(tmp_path, monkeypatch, heads=["1" * 40, "2" * 40])
        assert self._run(_mod(), clone, _entry(), monkeypatch) == 40
        assert "this is the 3rd fix of one span" in capsys.readouterr().err

    def test_the_head_being_fixed_is_not_counted_twice(self, clone, tmp_path, monkeypatch):
        # its disposition may already be posted before the push
        _fake_gh(tmp_path, monkeypatch, heads=["1" * 40, "2" * 40], head_now="2" * 40)
        assert self._run(_mod(), clone, _entry(), monkeypatch) == 0

    def test_one_fix_so_far_is_the_second_fix(self, clone, tmp_path, monkeypatch):
        _fake_gh(tmp_path, monkeypatch, heads=["1" * 40])
        assert self._run(_mod(), clone, _entry(), monkeypatch) == 0


PUSHED = [format(i, "x") * 40 for i in range(1, 8)]
LANE_RED = [{"name": "GPT 6.1 Review", "conclusion": "failure", "status": "completed"}]


def _blocked_thread(n):
    """A timeline of n+1 force pushes where the first n heads each failed a review lane."""
    events = [{"event": "head_ref_force_pushed", "commit_id": h} for h in PUSHED[: n + 1]]
    return {"timeline": events, "checks": {h: LANE_RED for h in PUSHED[:n]}}


class TestPrLevelPushCount:
    """No marker anywhere: the pushes that followed a blocked head speak for the PR."""

    def test_the_constants_match_the_contract_and_the_check_stays_warn_only(self):
        mod = _mod()
        contract = mod._sibling("_review_contract.py")
        assert (mod.BLOCKED_PUSH_REPEAT, mod.BLOCKED_PUSH_LIMIT) == (
            contract.BLOCKED_PUSH_REPEAT,
            contract.BLOCKED_PUSH_LIMIT,
        )
        assert mod.SWEEP_ENFORCED is False

    @pytest.mark.parametrize("pushes", [None, 0, 2])
    def test_below_three_pushes_or_unreadable_adds_no_finding(self, clone, monkeypatch, pushes):
        assert _check(_mod(), clone, _entry(), monkeypatch, counts={}, pushes=pushes) == 0

    @pytest.mark.parametrize("pushes", [3, 4])
    def test_three_pushes_apply_the_repeat_text(self, clone, monkeypatch, capsys, pushes):
        rc, err = _refused(_mod(), clone, _entry(), monkeypatch, capsys, counts={}, pushes=pushes)
        assert rc == 40
        nth = "4th" if pushes == 3 else "5th"
        assert f"this is the {nth} push of this PR after a blocked head: change the approach" in err
        assert "record `resolution:`, or patch once with a `branches:` table" in err

    def test_a_branches_table_or_a_resolution_answers_the_repeat_text(self, clone, monkeypatch):
        table = (
            "- final drain, first PUT fails  fixed",
            "- slot mid-cycle  covered-by test/test_a.py",
        )
        mod = _mod()
        assert _check(mod, clone, _entry(branches=table), monkeypatch, counts={}, pushes=3) == 0
        extra = "resolution: narrowed -- redo the hook lookup without the include path\n"
        assert _check(mod, clone, _entry(extra=extra), monkeypatch, counts={}, pushes=4) == 0

    @pytest.mark.parametrize("pushes", [5, 8])
    def test_five_pushes_need_a_resolution_and_a_table_is_not_enough(
        self, clone, monkeypatch, capsys, pushes
    ):
        table = (
            "- final drain, first PUT fails  fixed",
            "- slot mid-cycle  covered-by test/test_a.py",
        )
        mod = _mod()
        rc, err = _refused(
            mod, clone, _entry(branches=table), monkeypatch, capsys, counts={}, pushes=pushes
        )
        assert rc == 40
        assert "push of this PR after a blocked head: narrow the scope or defer it" in err
        extra = (
            "resolution: removed -- the second writer is gone and the hook list has one source\n"
        )
        assert _check(mod, clone, _entry(extra=extra), monkeypatch, counts={}, pushes=pushes) == 0

    def test_the_marker_count_is_used_first_when_it_reaches_a_seam(
        self, clone, monkeypatch, capsys
    ):
        rc, err = _refused(_mod(), clone, _entry(), monkeypatch, capsys, counts={SPAN: 2}, pushes=9)
        assert rc == 40
        assert "this is the 3rd fix of one span" in err and "push of this PR" not in err

    def test_unreadable_rounds_still_get_the_push_finding(self, clone, monkeypatch, capsys):
        rc, err = _refused(_mod(), clone, _entry(), monkeypatch, capsys, counts=None, pushes=3)
        assert rc == 40
        assert "seam rules skipped" in err and "4th push of this PR after a blocked head" in err

    def test_with_no_sweep_file_three_pushes_warn_and_the_exit_stays_zero(self, clone, capsys):
        mod = _mod()
        git_dir = _git(clone, "rev-parse", "--absolute-git-dir")
        rc = mod.run("main", git_dir, cwd=clone, prior_counts=lambda: {}, blocked_pushes=lambda: 3)
        assert rc == 0, "SWEEP_ENFORCED is False: the finding prints and nothing is refused"
        err = capsys.readouterr().err
        assert "WARNING (warn period, not refused)" in err
        assert "no sweep file: this is the 4th push of this PR after a blocked head" in err
        row = json.loads(Path(git_dir, "prepare-pr-metrics.jsonl").read_text().splitlines()[-1])
        assert row["outcome"] == "warned" and row["entries"] == 0 and row["findings"] == 1

    def test_with_no_sweep_file_the_finding_would_refuse_once_enforced(
        self, clone, monkeypatch, capsys
    ):
        mod = _mod()
        monkeypatch.setattr(mod, "SWEEP_ENFORCED", True)
        git_dir = _git(clone, "rev-parse", "--absolute-git-dir")
        rc = mod.run("main", git_dir, cwd=clone, prior_counts=lambda: {}, blocked_pushes=lambda: 5)
        assert rc == 40
        assert "no sweep file: this is the 6th push" in capsys.readouterr().err

    @pytest.mark.parametrize("pushes", [None, 0, 2])
    def test_with_no_sweep_file_fewer_pushes_stay_silent(self, clone, capsys, pushes):
        mod = _mod()
        git_dir = _git(clone, "rev-parse", "--absolute-git-dir")
        rc = mod.run(
            "main", git_dir, cwd=clone, prior_counts=lambda: {}, blocked_pushes=lambda: pushes
        )
        assert rc == 0 and capsys.readouterr() == ("", "")

    @pytest.mark.parametrize(
        "text", ["", "\n\n", "# Class sweep\n", "# Class sweep\n\n(none yet)\n"]
    )
    def test_a_file_with_no_entry_gets_the_pr_level_finding_not_a_clean_line(
        self, clone, monkeypatch, capsys, text
    ):
        rc, err = _refused(
            _mod(), clone, text, monkeypatch, capsys, enforced=False, counts={}, pushes=3
        )
        assert rc == 0
        assert "WARNING (warn period, not refused)" in err
        assert "no sweep entries: this is the 4th push of this PR after a blocked head" in err
        assert "change the approach" in err

    def test_a_file_with_no_entry_keeps_its_parse_findings_beside_the_pr_level_one(
        self, clone, monkeypatch, capsys
    ):
        text = "## span = abc\n"
        rc, err = _refused(_mod(), clone, text, monkeypatch, capsys, counts={}, pushes=5)
        assert rc == 40
        assert "a span heading is exactly" in err
        assert "no sweep entries: this is the 6th push of this PR after a blocked head" in err
        assert "narrow the scope or defer it" in err

    @pytest.mark.parametrize("pushes", [None, 0, 2])
    def test_a_file_with_no_entry_and_fewer_pushes_is_still_clean(
        self, clone, monkeypatch, capsys, pushes
    ):
        rc = _check(_mod(), clone, "", monkeypatch, counts={}, pushes=pushes)
        assert rc == 0
        assert "0 sweep entries checked, no findings" in capsys.readouterr().out

    def test_an_entry_in_the_file_does_not_also_get_the_no_entry_finding(
        self, clone, monkeypatch, capsys
    ):
        rc, err = _refused(_mod(), clone, _entry(), monkeypatch, capsys, counts={}, pushes=3)
        assert rc == 40 and "no sweep entries" not in err

    def test_an_empty_file_end_to_end_through_gh(self, clone, tmp_path, monkeypatch, capsys):
        _fake_gh(tmp_path, monkeypatch, heads=[], **_blocked_thread(3))
        assert self._run(_mod(), clone, monkeypatch, sweep="") == 40
        assert "no sweep entries: this is the 4th push" in capsys.readouterr().err

    def _run(self, mod, clone_dir, monkeypatch, sweep=None):
        git_dir = _git(clone_dir, "rev-parse", "--absolute-git-dir")
        if sweep is not None:
            _write_sweep(git_dir, sweep)
        monkeypatch.setattr(mod, "SWEEP_ENFORCED", True)
        guard = mod._sibling("push_guard.py")
        real = guard.tool_argv

        def argv(args):
            if args[0] == "gh":
                return [sys.executable, os.environ["FAKE_GH_SCRIPT"], *args[1:]]
            return real(args)

        monkeypatch.setattr(guard, "tool_argv", argv)
        return mod.run("main", git_dir, cwd=clone_dir)

    def test_the_default_reads_the_timeline_and_check_runs_through_gh(
        self, clone, tmp_path, monkeypatch, capsys
    ):
        _fake_gh(tmp_path, monkeypatch, heads=[], **_blocked_thread(3))
        assert self._run(_mod(), clone, monkeypatch, sweep=_entry()) == 40
        assert "this is the 4th push of this PR after a blocked head" in capsys.readouterr().err

    def test_the_default_with_no_sweep_file_end_to_end(self, clone, tmp_path, monkeypatch, capsys):
        _fake_gh(tmp_path, monkeypatch, heads=[], **_blocked_thread(5))
        assert self._run(_mod(), clone, monkeypatch) == 40
        assert "no sweep file: this is the 6th push" in capsys.readouterr().err

    def test_two_blocked_pushes_end_to_end_find_nothing(self, clone, tmp_path, monkeypatch):
        _fake_gh(tmp_path, monkeypatch, heads=[], **_blocked_thread(2))
        assert self._run(_mod(), clone, monkeypatch, sweep=_entry()) == 0

    def test_a_red_test_shard_on_green_lane_heads_finds_nothing(self, clone, tmp_path, monkeypatch):
        shard = [{"name": "Backend Tests (3.12, 2)", "conclusion": "failure"}]
        events = [{"event": "head_ref_force_pushed", "commit_id": h} for h in PUSHED[:6]]
        _fake_gh(
            tmp_path, monkeypatch, heads=[], timeline=events, checks={h: shard for h in PUSHED}
        )
        assert self._run(_mod(), clone, monkeypatch, sweep=_entry()) == 0

    def test_a_pr_gh_cannot_read_is_unknown(self, clone, monkeypatch):
        mod = _mod()
        monkeypatch.setattr(mod, "_run", lambda args, cwd=None, timeout=0: (1, "", "no PR"))
        assert mod._blocked_pushes(str(clone)) is None


class TestSearchSemantics:
    """The search means what it meant when the mate ran it, in both checks."""

    def test_git_default_basic_regex_alternation_is_kept(self, clone, monkeypatch):
        text = _entry(search=r"git grep -n 'hooksPath\|nothingElse' -- src/kiro_crew")
        assert _check(_mod(), clone, text, monkeypatch, counts={}) == 0

    def test_the_same_text_under_dash_e_is_a_literal_pipe_and_vacuous(
        self, clone, monkeypatch, capsys
    ):
        text = _entry(search=r"git grep -nE 'hooksPath\|nothingElse' -- src/kiro_crew")
        rc, err = _refused(_mod(), clone, text, monkeypatch, capsys, counts={})
        assert rc == 40 and "matches nothing in the tree before the fix" in err

    def test_dash_e_alternation_still_works(self, clone, monkeypatch):
        text = _entry(search="git grep -nE 'hooksPath|nothingElse' -- src/kiro_crew")
        assert _check(_mod(), clone, text, monkeypatch, counts={}) == 0

    def test_a_fixed_string_search_is_literal(self, clone, monkeypatch, capsys):
        text = _entry(search="git grep -nF 'hooks.Path' -- src/kiro_crew")
        rc, err = _refused(_mod(), clone, text, monkeypatch, capsys, counts={})
        assert rc == 40 and "matches nothing" in err

    def test_the_branch_as_last_pushed_is_a_pre_fix_tree(self, clone, monkeypatch):
        # the token exists only in a head that was pushed, then removed by the fix
        _commit_tree(clone, {A: "x = 1\nlegacy_token = 1\nhooks_path = 1\n"}, "fix: a2")
        _git(clone, "push", "-q", "origin", "feature/x")
        _commit_tree(clone, {A: "x = 1\nhooks_path = 1\n"}, "fix: a3")
        text = _entry(
            search="git grep -nE legacy_token -- src/kiro_crew", sites=(f"- {A}:2  fixed",)
        )
        assert _check(_mod(), clone, text, monkeypatch, counts={}) == 0

    def test_an_unpushed_token_is_vacuous(self, clone, monkeypatch, capsys):
        text = _entry(search="git grep -nE hooks_path -- src/kiro_crew", sites=(f"- {A}:2  fixed",))
        rc, err = _refused(_mod(), clone, text, monkeypatch, capsys, counts={})
        assert rc == 40 and "matches nothing" in err


class TestRunsFromTheRepositoryRoot:
    def test_a_check_started_in_a_subdirectory_reads_root_relative_searches(
        self, clone, monkeypatch
    ):
        sub = Path(clone, "src", "kiro_crew")
        assert _check(_mod(), str(sub), _entry(), monkeypatch, counts={}) == 0

    def test_a_check_started_in_a_nested_directory_refuses_the_same_findings(
        self, clone, monkeypatch, capsys
    ):
        sub = Path(clone, "src")
        sites = (f"- {A}:2  fixed",)
        rc, err = _refused(_mod(), str(sub), _entry(sites=sites), monkeypatch, capsys, counts={})
        assert rc == 40 and f"{B}:2" in err


class TestExecutableResolution:
    """git and gh come from PATH's absolute entries, never from the checkout."""

    @pytest.mark.parametrize("name", ["git", "gh"])
    def test_a_program_planted_in_the_checkout_is_never_run(self, tmp_path, monkeypatch, name):
        planted = tmp_path / name
        marker = tmp_path / "ran"
        _write_script(planted, marker)
        monkeypatch.setenv("PATH", ".")
        rc, _, err = _mod()._run([name, "--version"], cwd=str(tmp_path))
        assert rc == 127 and not marker.exists()
        assert f"{name}-not-on-PATH" in err

    def test_the_resolved_git_is_absolute(self):
        argv = _mod()._sibling("push_guard.py").tool_argv(["git", "status"])
        assert os.path.isabs(argv[0]) and argv[1:] == ["status"]

    def test_only_git_and_gh_may_be_run(self):
        with pytest.raises(ValueError):
            _mod()._sibling("push_guard.py").tool_argv(["sh", "-c", "true"])
        rc, _, err = _mod()._run(["sh", "-c", "true"])
        assert rc == 127 and "only git and gh" in err

    def test_the_resolver_is_push_guards_own(self):
        assert _mod()._sibling("push_guard.py")._which("git") == _load_push_guard()._which_git()

    def test_sweep_check_has_no_launch_code_of_its_own(self):
        src = SCRIPT.read_text(encoding="utf-8")
        assert "subprocess" not in src and "Popen" not in src
        assert "run_child(" in src


class TestPlatformHelpers:
    """The stand-ins and sweep writes that keep these tests the same on Windows and POSIX."""

    def test_the_posix_script_creates_its_marker_and_is_executable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        script, marker = tmp_path / "tool", tmp_path / "ran"
        _write_script(script, marker)
        assert script.read_bytes() == f"#!/bin/sh\ntouch '{marker}'\n".encode("ascii")
        if os.name == "posix":  # Windows has no execute bit; its stand-in is the batch path
            assert script.stat().st_mode & stat.S_IXUSR
        _write_script(tmp_path / "quiet")
        assert (tmp_path / "quiet").read_bytes() == b"#!/bin/sh\nexit 0\n"

    def test_the_windows_script_is_a_batch_file_with_the_same_effect(self, tmp_path, monkeypatch):
        script, quiet, marker = tmp_path / "tool.cmd", tmp_path / "quiet.cmd", tmp_path / "ran"
        monkeypatch.setattr(sys, "platform", "win32")
        _write_script(script, marker)
        _write_script(quiet)
        assert script.read_bytes() == f'@echo off\r\ntype nul > "{marker}"\r\n'.encode("ascii")
        assert quiet.read_bytes() == b"@echo off\r\nexit /b 0\r\n"

    def test_the_sweep_file_keeps_its_exact_bytes(self, tmp_path):
        _write_sweep(str(tmp_path), "a\r\nb\nc\rd\u2028e")
        assert Path(tmp_path, "prepare-pr-sweep.md").read_bytes() == b"a\r\nb\nc\rd\xe2\x80\xa8e"


class TestBatchShimLaunch:
    """A .cmd/.bat git or gh is run by cmd.exe, which executes `&` in an argument."""

    def _shim(self, tmp_path, monkeypatch, name):
        marker = tmp_path / "ran"
        shim = tmp_path / (name + ".cmd")
        _write_script(shim, marker)
        mod = _mod()
        guard = mod._sibling("push_guard.py")
        monkeypatch.setattr(guard, "tool_argv", lambda args: [str(shim), *args[1:]])
        return mod, marker

    @pytest.mark.parametrize("meta", ["&", "|", "^", "<", ">", "%", "!", '"'])
    def test_a_search_pattern_with_a_cmd_metacharacter_is_refused_not_run(
        self, tmp_path, monkeypatch, meta
    ):
        mod, marker = self._shim(tmp_path, monkeypatch, "git")
        spec = {"flags": set(), "pattern": f"hooksPath{meta}calc{meta}", "paths": ["src"]}
        hits, bad = mod._grep(spec, "HEAD", str(tmp_path))
        assert hits == [] and "refusing batch-file launch" in bad
        assert not marker.exists()

    def test_a_gh_batch_shim_is_refused_the_same_way(self, tmp_path, monkeypatch):
        mod, marker = self._shim(tmp_path, monkeypatch, "gh")
        rc, _, err = mod._run(["gh", "api", "repos/o/r/issues/1/comments?per_page=100&page=1"])
        assert rc == 126 and "refusing batch-file launch" in err and not marker.exists()

    def test_a_clean_argument_still_reaches_the_shim(self, tmp_path, monkeypatch):
        mod, marker = self._shim(tmp_path, monkeypatch, "git")
        rc, _, _ = mod._run(["git", "status"])
        assert rc == 0 and marker.exists()

    def test_the_refusal_is_a_finding_on_a_real_search(self, clone, monkeypatch, capsys):
        mod = _mod()
        guard = mod._sibling("push_guard.py")
        real = guard.tool_argv
        shim = Path(clone, "..", "git.cmd").resolve()
        _write_script(shim)

        def argv(args):
            if args[:2] == ["git", "grep"]:
                return [str(shim), *args[1:]]
            return real(args)

        monkeypatch.setattr(guard, "tool_argv", argv)
        text = _entry(search="git grep -nF 'hooksPath&calc&' -- src/kiro_crew")
        git_dir = _git(clone, "rev-parse", "--absolute-git-dir")
        _write_sweep(git_dir, text)
        monkeypatch.setattr(mod, "SWEEP_ENFORCED", True)
        rc = mod.run(
            "main", git_dir, cwd=clone, prior_counts=lambda: {}, blocked_pushes=lambda: None
        )
        assert rc == 40 and "refusing batch-file launch" in capsys.readouterr().err


class TestSearchParsing:
    """Site (b): a pattern or flag set is refused unless it means one thing to git and to us."""

    @pytest.mark.parametrize(
        "search",
        [
            "git grep -nFE 'a|b' -- src/kiro_crew",
            "git grep -nEF 'a|b' -- src/kiro_crew",
            "git grep -E -F hooksPath -- src/kiro_crew",
            "git grep -F -n -E hooksPath -- src/kiro_crew",
        ],
    )
    def test_both_regex_modes_in_one_search_are_refused(self, clone, monkeypatch, capsys, search):
        rc, err = _refused(_mod(), clone, _entry(search=search), monkeypatch, capsys, counts={})
        assert rc == 40 and "carries both -E and -F" in err

    def test_a_single_mode_is_kept_whatever_the_flag_order(self, clone, monkeypatch):
        for search in (
            "git grep -nFi hooksPATH -- src/kiro_crew",
            "git grep -n -E 'hooksPath|zzz' -- src/kiro_crew",
            "git grep -nE -i 'HOOKSPATH|zzz' -- src/kiro_crew",
        ):
            assert _check(_mod(), clone, _entry(search=search), monkeypatch, counts={}) == 0, search

    def test_a_pattern_with_a_newline_is_refused(self):
        kind, why = _mod().parse_search("git grep -nE 'a\nb' -- src")
        assert kind == "error" and "newline" in why
        kind, spec = _mod().parse_search("git grep -nE 'ab' -- src")
        assert kind == "grep" and spec["pattern"] == "ab"


class TestSiteRows:
    """Site (c): a row covers exactly what it names."""

    def _two_hits(self, clone):
        _commit_tree(
            clone,
            {B: "def f():\n    return core.hooksPath\n    return other.hooksPath\n"},
            "fix: b",
        )

    def test_a_line_qualified_row_covers_only_its_line(self, clone, monkeypatch, capsys):
        self._two_hits(clone)
        sites = (f"- {A}:2  fixed", f"- {B}:2  not-in-class -- {REASON}")
        rc, err = _refused(_mod(), clone, _entry(sites=sites), monkeypatch, capsys, counts={})
        assert rc == 40 and "1 hit(s) no site line accounts for: " + f"{B}:3" in err

    def test_each_hit_line_listed_passes(self, clone, monkeypatch):
        self._two_hits(clone)
        sites = (
            f"- {A}:2  fixed",
            f"- {B}:2  not-in-class -- {REASON}",
            f"- {B}:3  not-in-class -- {REASON}",
        )
        assert _check(_mod(), clone, _entry(sites=sites), monkeypatch, counts={}) == 0

    def test_a_path_only_row_covers_the_whole_file(self, clone, monkeypatch):
        self._two_hits(clone)
        sites = (f"- {A}  fixed", f"- {B}  not-in-class -- {REASON}")
        assert _check(_mod(), clone, _entry(sites=sites), monkeypatch, counts={}) == 0

    def test_a_line_row_in_another_file_covers_nothing_here(self, clone, monkeypatch, capsys):
        sites = (f"- {A}:2  fixed", f"- {A}:9  not-in-class -- {REASON}")
        rc, err = _refused(_mod(), clone, _entry(sites=sites), monkeypatch, capsys, counts={})
        assert rc == 40 and f"{B}:2" in err

    def test_non_ascii_and_spaced_file_names_are_matched_verbatim(self, clone, monkeypatch):
        odd = "src/kiro_crew/my é file.py"
        _commit_tree(clone, {odd: "x = 1\nhooksPath = 2\n"}, "fix: odd")
        sites = (f"- {A}:2  fixed", f"- {B}:2  not-in-class -- {REASON}", f"- {odd}  fixed")
        assert _check(_mod(), clone, _entry(sites=sites), monkeypatch, counts={}) == 0

    @needs_posix_names
    def test_a_file_name_with_colons_and_digits_is_matched_verbatim(self, clone, monkeypatch):
        odd = "src/kiro_crew/my é:12:file.py"
        _commit_tree(clone, {odd: "x = 1\nhooksPath = 2\n"}, "fix: odd")
        sites = (f"- {A}:2  fixed", f"- {B}:2  not-in-class -- {REASON}", f"- {odd}  fixed")
        assert _check(_mod(), clone, _entry(sites=sites), monkeypatch, counts={}) == 0

    def test_the_hit_parser_reads_any_name_git_can_print_without_a_filesystem(self, monkeypatch):
        mod = _mod()
        names = [
            "src/a:12:b.py",
            "src/line\nbreak.py",
            "src/tab\tname.py",
            "src/my é.py",
            "src/sp ace.py",
        ]
        stream = "".join(f"HEAD:{n}\0{i + 1}\0text 7: x\n" for i, n in enumerate(names))
        monkeypatch.setattr(mod, "_git", lambda args, cwd=None, timeout=0: (0, stream, ""))
        hits, bad = mod._grep({"flags": set(), "pattern": "x", "paths": ["src"]}, "HEAD", ".")
        assert bad == "" and hits == [(n, i + 1) for i, n in enumerate(names)]

    def test_split_site_reads_an_optional_trailing_line(self):
        split = _mod()._split_site
        assert split("src/a.py:12") == ("src/a.py", 12)
        assert split("src/a.py") == ("src/a.py", None)
        assert split("src/a:12:b.py") == ("src/a:12:b.py", None)
        assert split("src/my file.py:7") == ("src/my file.py", 7)


class TestNamesFromTheSweep:
    """Site (d): a test name or path from the sweep file is checked against HEAD and the diff."""

    @pytest.mark.parametrize(
        "test_id", ["test/test_a.py::test_hook", "test/test_a.py::hooks", "test/test_a.py::tes"]
    )
    def test_a_test_name_must_match_whole_not_as_a_substring(
        self, clone, monkeypatch, capsys, test_id
    ):
        sites = (f"- {A}:2  fixed", f"- {B}:2  covered-by {test_id}")
        rc, err = _refused(_mod(), clone, _entry(sites=sites), monkeypatch, capsys, counts={})
        assert rc == 40 and "has no" in err

    def test_a_parametrized_id_matches_its_function(self, clone, monkeypatch):
        sites = (f"- {A}:2  fixed", f"- {B}:2  covered-by test/test_a.py::test_hooks[case-1]")
        assert _check(_mod(), clone, _entry(sites=sites), monkeypatch, counts={}) == 0

    @pytest.mark.parametrize(
        "test_id", ["../test/test_a.py::test_hooks", "/etc/passwd", "test/../test/test_a.py"]
    )
    def test_a_path_that_is_not_a_repo_relative_diff_path_is_refused(
        self, clone, monkeypatch, capsys, test_id
    ):
        sites = (f"- {A}:2  fixed", f"- {B}:2  covered-by {test_id}")
        rc, err = _refused(_mod(), clone, _entry(sites=sites), monkeypatch, capsys, counts={})
        assert rc == 40 and "is not in the diff" in err

    def test_a_path_with_a_space_in_the_diff_is_found(self, clone, monkeypatch):
        spaced = "src/kiro_crew/two words.py"
        _commit_tree(clone, {spaced: "x = 1\n"}, "fix: spaced")
        sites = (f"- {A}:2  fixed", f"- {B}:2  not-in-class -- {REASON}", f"- {spaced}  fixed")
        assert _check(_mod(), clone, _entry(sites=sites), monkeypatch, counts={}) == 0


class TestCountsFromThePr:
    """Site (e): the same record rules as --rounds, on any spelling of a head."""

    def _rec(self, cid, head, span=SPAN):
        return {"comment_id": cid, "head": head, "spans": [span], "malformed": False}

    def test_a_short_and_a_full_spelling_of_one_head_count_once(self):
        mod = _mod()
        full = "abcdef0123456789abcdef0123456789abcdef01"
        records = [self._rec(1, full), self._rec(2, full[:7]), self._rec(3, full[:12])]
        bodies = {1: "**fixed** -- a\n", 2: "**fixed** -- b\n", 3: "**fixed** -- c\n"}
        assert mod.fix_counts_from(records, bodies) == {SPAN: 1}

    def test_two_different_heads_still_count_twice(self):
        mod = _mod()
        records = [self._rec(1, "abcdef0" + "1" * 33), self._rec(2, "abcdef1" + "1" * 33)]
        bodies = {1: "**fixed** -- a\n", 2: "**fixed** -- b\n"}
        assert mod.fix_counts_from(records, bodies) == {SPAN: 2}

    def test_the_head_being_fixed_is_skipped_under_any_spelling(self):
        mod = _mod()
        full = "abcdef0123456789abcdef0123456789abcdef01"
        records = [self._rec(1, full[:7]), self._rec(2, "9" * 40)]
        bodies = {1: "**fixed** -- a\n", 2: "**fixed** -- b\n"}
        assert mod.fix_counts_from(records, bodies, skip_head=full) == {SPAN: 1}


class TestDelimiters:
    """Every whitespace-, separator- and delimiter-sensitive site, one test each."""

    # 1-2: a row's keyword and argument, whatever whitespace separates them
    @pytest.mark.parametrize("sep", ["\t", "  ", " \t ", "\u00a0"])
    def test_covered_by_checks_run_whatever_whitespace_follows_the_keyword(
        self, clone, monkeypatch, capsys, sep
    ):
        sites = (f"- {A}:2  fixed", f"- {B}:2  covered-by{sep}test/nope.py::t")
        rc, err = _refused(_mod(), clone, _entry(sites=sites), monkeypatch, capsys, counts={})
        assert rc == 40 and "test file test/nope.py is not in the diff" in err

    @pytest.mark.parametrize("keyword", ["not-in-class", "single"])
    @pytest.mark.parametrize("sep", ["\t--\t", " \t-- ", "\t-- \t"])
    def test_short_reasons_are_refused_whatever_whitespace_surrounds_the_dashes(
        self, clone, monkeypatch, capsys, keyword, sep
    ):
        row = f"- {B}:2  {keyword}{sep}short"
        rows = (f"- {A}:2  fixed", row)
        if keyword == "single":
            text = _entry(search="none -- no common token", sites=(), branches=rows)
        else:
            text = _entry(sites=rows)
        rc, err = _refused(_mod(), clone, text, monkeypatch, capsys, counts={})
        assert rc == 40 and "needs a reason of 20+ characters" in err

    def test_a_tab_separated_row_that_is_valid_passes(self, clone, monkeypatch):
        sites = (f"- {A}:2\tfixed", f"- {B}:2\tnot-in-class\t--\t{REASON}")
        assert _check(_mod(), clone, _entry(sites=sites), monkeypatch, counts={}) == 0

    def test_a_reason_that_holds_dashes_is_kept_whole(self):
        entries, problems = _mod().parse_sweep(
            f"## span={SPAN}\nclass: c\nsites:\n- a.py  not-in-class -- long -- reason with -- dashes\n"
        )
        assert problems == []
        assert entries[0]["sites"][0]["arg"] == "long -- reason with -- dashes"

    def test_row_parts_come_from_named_groups_not_a_split(self):
        src = SCRIPT.read_text(encoding="utf-8")
        assert 'partition(" ")' not in src and 'split("--"' not in src
        rows = {
            "- a.py  fixed": ("fixed", ""),
            "- a.py\tcovered-by\tt/x.py::t": ("covered-by", "t/x.py::t"),
            "- a.py  not-in-class  --  because of a long reason  ": (
                "not-in-class",
                "because of a long reason",
            ),
            "- a.py  single\t--\tone writer only here": ("single", "one writer only here"),
        }
        for line, want in rows.items():
            entries, problems = _mod().parse_sweep(f"## span={SPAN}\nsites:\n{line}\n")
            assert problems == [], line
            row = entries[0]["sites"][0]
            assert (row["status"], row["arg"]) == want, line

    # 3: a reason is counted after whitespace collapses
    def test_padding_does_not_make_a_short_reason_long(self, clone, monkeypatch, capsys):
        sites = (f"- {A}:2  fixed", f"- {B}:2  not-in-class -- ok{' ' * 30}ok")
        rc, err = _refused(_mod(), clone, _entry(sites=sites), monkeypatch, capsys, counts={})
        assert rc == 40 and "needs a reason of 20+ characters" in err

    # 4-5: line ends
    def test_only_cr_lf_and_crlf_end_a_line(self):
        entries, problems = _mod().parse_sweep(
            f"## span={SPAN}\r\nclass: a\u2028still the class line\r\nsearch: none -- x\rsites:\n"
        )
        assert problems == []
        assert entries[0]["class"] == "a\u2028still the class line"
        assert entries[0]["search"] == "none -- x"

    def test_a_crlf_sweep_file_passes(self, clone, monkeypatch):
        text = _entry().replace("\n", "\r\n")
        assert _check(_mod(), clone, text, monkeypatch, counts={}) == 0

    # 6-7: headings and BOM
    @pytest.mark.parametrize(
        "heading",
        ["##  span=abcdef012345", "# span=abcdef012345", "## span=ABCDEF012345", "## span = abc"],
    )
    def test_a_near_miss_span_heading_is_a_finding_not_silence(
        self, clone, monkeypatch, capsys, heading
    ):
        text = _entry() + f"\n{heading}\nclass: another\nsearch: none -- x\n"
        rc, err = _refused(_mod(), clone, text, monkeypatch, capsys, counts={})
        assert rc == 40 and "a span heading is exactly" in err

    def test_a_leading_bom_is_ignored(self, clone, monkeypatch):
        assert _check(_mod(), clone, "\ufeff" + _entry(), monkeypatch, counts={}) == 0

    # 8: risk values
    @pytest.mark.parametrize("value", ["Security", "security, data-loss", "data-loss|corruption"])
    def test_every_spelling_of_a_sensitive_risk_blocks_a_deferral(
        self, clone, monkeypatch, capsys, value
    ):
        extra = f"risk: {value}\nresolution: deferred #4711 pinned-by test/test_a.py::test_hooks\n"
        rc, err = _refused(_mod(), clone, _entry(extra=extra), monkeypatch, capsys, counts={})
        assert rc == 40 and "is never deferred" in err

    def test_an_unknown_risk_word_is_a_finding(self, clone, monkeypatch, capsys):
        extra = "risk: secuirty\n"
        rc, err = _refused(_mod(), clone, _entry(extra=extra), monkeypatch, capsys, counts={})
        assert rc == 40 and "risk must name security, data-loss or corruption, not secuirty" in err

    # 9: parameter ids
    def test_a_double_colon_inside_a_parameter_is_not_a_separator(self, clone, monkeypatch):
        sites = (f"- {A}:2  fixed", f"- {B}:2  covered-by test/test_a.py::test_hooks[a::b]")
        assert _check(_mod(), clone, _entry(sites=sites), monkeypatch, counts={}) == 0

    def test_a_missing_name_after_the_separator_is_refused(self, clone, monkeypatch, capsys):
        sites = (f"- {A}:2  fixed", f"- {B}:2  covered-by test/test_a.py::[a]")
        rc, err = _refused(_mod(), clone, _entry(sites=sites), monkeypatch, capsys, counts={})
        assert rc == 40 and "has no test named" in err

    # 10: a leading ./
    def test_a_leading_dot_slash_names_the_same_file(self, clone, monkeypatch):
        sites = (f"- ./{A}:2  fixed", f"- ./{B}:2  covered-by ./test/test_a.py::test_hooks")
        assert _check(_mod(), clone, _entry(sites=sites), monkeypatch, counts={}) == 0
        assert _mod()._split_site("./././src/a.py:3") == ("src/a.py", 3)
        assert _mod()._repo_path("../x.py") == "../x.py"

    # 11: a file name with a newline in the hit stream
    @needs_posix_names
    def test_a_file_name_with_a_newline_is_read_whole_and_listed_not_dropped(
        self, clone, monkeypatch, capsys
    ):
        odd = "src/kiro_crew/line\nbreak.py"
        _commit_tree(clone, {odd: "hooksPath = 2\n"}, "fix: newline name")
        # a row is one line, so no row can name this file: it is listed as unaccounted
        sites = (f"- {A}:2  fixed", f"- {B}:2  not-in-class -- {REASON}")
        rc, err = _refused(_mod(), clone, _entry(sites=sites), monkeypatch, capsys, counts={})
        assert rc == 40 and "1 hit(s) no site line accounts for" in err and "break.py:1" in err
        hits, bad = _mod()._grep(
            {"flags": set(), "pattern": "hooksPath", "paths": ["src"]}, "HEAD", clone
        )
        assert bad == "" and (odd, 1) in hits

    # 12-13: fences and the bold outcome label

    # 14-15: parts that already work, pinned
    def test_fields_and_the_search_line_accept_tabs_and_trailing_blanks(self, clone, monkeypatch):
        text = _entry(search="git\tgrep\t-nE\t'hooksPath'\t--\tsrc/kiro_crew   ")
        text = text.replace("class: ", "class:\t").replace("sites:", "sites:   ")
        assert _check(_mod(), clone, text, monkeypatch, counts={}) == 0

    def test_a_quoted_double_dash_is_still_the_separator_and_leaves_no_pattern(self):
        kind, why = _mod().parse_search("git grep '--' src")
        assert kind == "error" and "no pattern" in why
        kind, why = _mod().parse_search("none--reason")
        assert kind == "error"
        kind, why = _mod().parse_search("none \t--\t because of ordering")
        assert (kind, why) == ("none", "because of ordering")


class TestFences:
    """The opening fence's character and length are remembered; only a match closes it."""

    def test_sweep_check_keeps_no_fence_parser_of_its_own(self):
        src = SCRIPT.read_text(encoding="utf-8")
        assert "`{3,}" not in src and "FENCE_OPEN_RE" not in src and "LINE_END_RE" not in src
        assert "disposition_rounds" in src and "fix_rounds" in src and "split_lines(text)" in src
        assert "declared_outcome" not in src and "OUTCOME_" not in src

    def test_a_nested_fence_in_evidence_is_not_a_false_fix_in_the_counts(self):
        mod = _mod()
        body = "~~~markdown\n```\n- **fixed** quoted\n> Outcome: fixed in abc\n```\n~~~\n"
        body = "**rebutted** span=aaaaaaaaaaa1\n" + body + "> Outcome: rebutted, by design.\n"
        records = [
            {"comment_id": i, "head": str(i + 1) * 40, "spans": [SPAN], "malformed": False}
            for i in range(4)
        ]
        assert mod.fix_counts_from(records, {i: body for i in range(4)}) == {}


class TestCountsReadTheFormsWritersUse:
    """fix_counts_from reads the same forms as `pr_findings.py --rounds` (the contract's reader)."""

    @staticmethod
    def _records(n, span=SPAN):
        return [
            {"comment_id": i, "head": str(i + 1) * 40, "spans": [span], "malformed": False}
            for i in range(n)
        ]

    @pytest.mark.parametrize(
        "body",
        [
            "- **Title** (span={s})\n> fixed in 2d49e39956c1: the row now shows a key\n",
            "- **Title** (span={s})\n> Fixed in dcf637c69b97. The flag is set\n",
            "- **Title** (span={s})\n> **fixed** in debee13b98. detail\n",
            "- **Title** (span={s})\n> Outcome: fixed in 7ff6176c3d. detail\n",
            "- **fixed** span={s} -- Title\n> detail\n",
            "**fixed** -- span={s}\n> detail\n",
            "Disposition: fixed, span={s}, in the next push.\n> detail\n",
            "Outcome: fixed\n- **Title** (span={s})\n",
            "- **Title** (span={s})\n> removed in 3f04fb2f04e6: the parameter is gone\n",
        ],
    )
    def test_each_real_fix_format_counts(self, body):
        text = body.format(s=SPAN)
        counts = _mod().fix_counts_from(self._records(3), {i: text for i in range(3)})
        assert counts == {SPAN: 3}

    @pytest.mark.parametrize(
        "body",
        [
            "- **rebutted: Title** span={s}\n> fixed in an earlier head, quoted evidence\n",
            "- **acknowledged: Title** span={s}\n> detail\n",
            "- **Title** (span={s})\n> Agreed, no change.\n",
            "- **Title** (span={s})\n> Kept as two maps\n",
            "- **Title** (span={s})\n> Outcome: checked, no change needed.\n",
            "- **Title** (span={s})\n> Legitimate. detail\n",
            "- **Fixed point iteration is slow** (span={s})\n> detail\n",
            "- **fixed: Title** span={s}\n> thought: detail\n",
            "- **fixed span={s} -- Title**\n> detail\n",
            "- **Title** span={s} -- fixed in 359f7cdffa2b\n> detail\n",
            "First Principles review: item 6 (span={s}) is fixed in 049fcdcc6600.\n",
            "- **Deleted slot -- removed sessions still listed** (span={s})\n> Rebutted: by design.\n",
            "- **Retry: removed entries stay** (span={s})\n> Rebutted: by design.\n",
            "- **Title** (span={s})\n> Rebutted: by design.\nclass: a guard removed in abc1234\n",
            "- **Title** (span={s})\n> Rebutted: by design.\nresolution: removed in abc1234\n",
            "- **Title** (span={s})\n> Rebutted: by design.\nmechanism: fixed in abc1234\n",
            "- **Title** (span={s})\n> Rebutted: by design.\nself-added: removed in abc1234\n",
        ],
    )
    def test_a_non_fix_or_undeclared_record_never_counts(self, body):
        text = body.format(s=SPAN)
        assert _mod().fix_counts_from(self._records(3), {i: text for i in range(3)}) == {}

    def test_a_plain_resolution_line_restarts_the_count(self):
        fix = "- **fixed** span={s} -- Title\n> detail\n".format(s=SPAN)
        resolved = fix + "resolution: narrowed -- only the key is read now\n"
        bodies = {0: fix, 1: resolved, 2: fix, 3: fix}
        assert _mod().fix_counts_from(self._records(4), bodies) == {SPAN: 2}
        quoted = fix + "> resolution: narrowed -- quoted evidence, not a record\n"
        assert _mod().fix_counts_from(self._records(2), {0: quoted, 1: quoted}) == {SPAN: 2}

    def test_the_head_being_fixed_is_skipped_under_any_spelling(self):
        fix = "- **fixed** span={s} -- Title\n".format(s=SPAN)
        records = self._records(2)
        mod = _mod()
        assert mod.fix_counts_from(records, {0: fix, 1: fix}, skip_head="2" * 12) == {SPAN: 1}
        assert mod.fix_counts_from(records, {0: fix, 1: fix}, skip_head="2" * 40) == {SPAN: 1}

    def test_the_counter_is_the_contract_modules_own(self):
        mod = _mod()
        contract = mod._sibling("_review_contract.py")
        fix = "- **fixed** span={s} -- Title\n".format(s=SPAN)
        records = self._records(3)
        bodies = {i: fix for i in range(3)}
        order, rounds = contract.disposition_rounds(records, bodies)
        expected = {sp: len(rs) for sp, rs in contract.fix_rounds(order, rounds).items()}
        assert mod.fix_counts_from(records, bodies) == expected == {SPAN: 3}
