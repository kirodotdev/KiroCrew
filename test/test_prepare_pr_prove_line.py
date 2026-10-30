"""``prove.py`` prints the ``Proof:`` line a fix pastes under ``## Tests`` and records it.

``diff_signals.py --check-body`` compares a ``PROVEN`` line with the record for the
same tree, so a stale or invented line fails in the agent flow. These tests pin the
line, the record (tree-keyed), exit 11 for frontend tests, and the closed list of
"not provable" reasons the CI check accepts.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from git_maintenance_helpers import git_maintenance_off_entries
from skill_script_helpers import load_skill_script

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

ROOT = Path(__file__).resolve().parent.parent
PREPARE_PR = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "kirocrew-prepare-pr"
PROVE = PREPARE_PR / "scripts" / "prove.py"
CHECK = ROOT / ".github" / "scripts" / "proof_line_check.py"

prove = load_skill_script("prove_line_under_test", PROVE)
check = load_skill_script("proof_line_check_for_prove_test", CHECK)

LINE_RE = re.compile(r"^Proof: PROVEN (?P<tests>.+) \(head (?P<sha>[0-9a-f]{12})\)$", re.MULTILINE)


def _env() -> dict[str, str]:
    """The environment for every git and prove.py call: no inherited ``GIT_*`` at all.

    Run from a git hook, the parent exports ``GIT_DIR`` (and friends); one of them left in
    place would point every call at the hook's repository instead of ``tmp_path``. The
    identity comes from variables, so no ``user.*`` is ever written to any config.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        {
            "GIT_AUTHOR_NAME": "prove",
            "GIT_AUTHOR_EMAIL": "prove@test",
            "GIT_COMMITTER_NAME": "prove",
            "GIT_COMMITTER_EMAIL": "prove@test",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    # Dropping every GIT_* drops the suite's maintenance entries too, and the
    # global file is pointed away, so they go back on the env this helper built.
    env.update(git_maintenance_off_entries(env))
    return env


@pytest.fixture(autouse=True)
def _no_inherited_git_location(_floor_monkeypatch) -> None:
    """In-process calls (``prove.main``, ``prove.prove``) must not see a hook's ``GIT_DIR``.

    Only the location variables go. The identity and the ``GIT_CONFIG_GLOBAL`` /
    ``GIT_CONFIG_SYSTEM`` pins that ``conftest._git_identity`` sets stay, and are set again
    here, so no in-process git call reads the developer's real config or runs its hooks.
    """
    drop = _GIT_LOCATION_VARS | {"GIT_PREFIX", "GIT_CONFIG_PARAMETERS"}
    for key in [k for k in os.environ if k in drop]:
        _floor_monkeypatch.delenv(key)
    _floor_monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    _floor_monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    _floor_monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(),
    )
    return out.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "mod.py").write_text("def last_char(s):\n    return s[len(s) - 2]\n")
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\ndef test_smoke():\n    assert last_char('abc') == 'b'\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    return repo


def _run(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(PROVE), "--base", "main"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(),
    )


def _record(repo: Path) -> dict:
    gitdir = _git(repo, "rev-parse", "--absolute-git-dir")
    return json.loads((Path(gitdir) / prove.RECORD_FILENAME).read_text(encoding="utf-8"))


def _fix_with_control_test(repo: Path) -> None:
    """A fix with one test that fails without it and one control that passes both ways."""
    (repo / "mod.py").write_text("def last_char(s):\n    return s[len(s) - 1]\n")
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\n"
        "def test_last():\n    assert last_char('abc') == 'c'\n\n\n"
        "def test_returns_a_str():\n    assert isinstance(last_char('abc'), str)\n"
    )
    _git(repo, "commit", "-aqm", "fix off-by-one")


# --------------------------------------------------------------- green path


def test_proven_prints_the_line_naming_only_the_tests_that_fail(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _fix_with_control_test(repo)
    r = _run(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    m = LINE_RE.search(r.stdout)
    assert m, r.stdout
    assert m.group("tests") == "test_mod.py::test_last"  # the control test is not named
    assert m.group("sha") == _git(repo, "rev-parse", "HEAD")[:12]
    assert r.stdout.rstrip().splitlines()[-1] == m.group(0)


def test_the_record_is_keyed_by_tree_and_names_the_line(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _fix_with_control_test(repo)
    r = _run(repo)
    record = _record(repo)
    assert record["verdict"] == "PROVEN" and record["exit"] == 0
    assert record["tree"] == _git(repo, "rev-parse", "HEAD^{tree}")
    assert record["head"] == _git(repo, "rev-parse", "HEAD")
    assert record["tests"] == ["test_mod.py::test_last"]
    assert record["line"] in r.stdout


def test_a_record_written_by_prove_satisfies_the_check(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _fix_with_control_test(repo)
    line = LINE_RE.search(_run(repo).stdout).group(0)
    gitdir = _git(repo, "rev-parse", "--absolute-git-dir")
    record_path = str(Path(gitdir) / prove.RECORD_FILENAME)
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    body = f"## Tests\n\n{line}\n"
    problems, proven = check.check("fix: x", body)
    assert problems == [] and proven is not None
    assert check.compare_record(proven, record_path, tree) == []


def test_a_message_only_amend_keeps_the_record_valid(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _fix_with_control_test(repo)
    line = LINE_RE.search(_run(repo).stdout).group(0)
    _git(repo, "commit", "-q", "--amend", "-m", "fix: a better message")
    gitdir = _git(repo, "rev-parse", "--absolute-git-dir")
    _, proven = check.check("fix: x", f"## Tests\n\n{line}\n")
    assert (
        check.compare_record(
            proven,
            str(Path(gitdir) / prove.RECORD_FILENAME),
            _git(repo, "rev-parse", "HEAD^{tree}"),
        )
        == []
    )


# ----------------------------------------------------------------- red path


def test_a_stale_record_fails_the_comparison_after_the_code_changes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _fix_with_control_test(repo)
    line = LINE_RE.search(_run(repo).stdout).group(0)
    (repo / "mod.py").write_text("def last_char(s):\n    return s[-1]\n")
    _git(repo, "commit", "-aqm", "rewrite")
    gitdir = _git(repo, "rev-parse", "--absolute-git-dir")
    _, proven = check.check("fix: x", f"## Tests\n\n{line}\n")
    problems = check.compare_record(
        proven, str(Path(gitdir) / prove.RECORD_FILENAME), _git(repo, "rev-parse", "HEAD^{tree}")
    )
    assert problems and "different tree" in problems[0]


def test_an_invented_line_fails_the_comparison(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _fix_with_control_test(repo)
    _run(repo)
    gitdir = _git(repo, "rev-parse", "--absolute-git-dir")
    path = str(Path(gitdir) / prove.RECORD_FILENAME)
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    head = _git(repo, "rev-parse", "HEAD")[:12]
    for line, fragment in [
        (f"Proof: PROVEN test_mod.py::test_invented (head {head})", "prove.py recorded"),
        (f"Proof: PROVEN test_mod.py::test_last (head {'0' * 12})", "prove.py recorded"),
        (
            f"Proof: PROVEN test_mod.py::test_last, test_mod.py::test_ghost (head {head})",
            "prove.py recorded",
        ),
    ]:
        _, proven = check.check("fix: x", f"## Tests\n\n{line}\n")
        problems = check.compare_record(proven, path, tree)
        assert any(fragment in p for p in problems), (line, problems)


def _proven_record(tmp_path: Path) -> tuple[str, str, str]:
    """(record path, tree, the line prove.py printed) for a proven fix in a fresh repository."""
    repo = _repo(tmp_path)
    _fix_with_control_test(repo)
    line = LINE_RE.search(_run(repo).stdout).group(0)
    gitdir = _git(repo, "rev-parse", "--absolute-git-dir")
    return (
        str(Path(gitdir) / prove.RECORD_FILENAME),
        _git(repo, "rev-parse", "HEAD^{tree}"),
        line,
    )


def test_a_proven_record_cannot_be_answered_with_a_not_provable_line(tmp_path: Path) -> None:
    """The reviewer's case: prove.py proved it, the author writes an excuse instead."""
    path, tree, _ = _proven_record(tmp_path)
    for reason in ("test-only change", "docs or config only", "other: " + "x" * 25):
        line = f"Proof: not provable -- {reason}"
        problems, parsed = check.check("fix: x", f"## Tests\n\n{line}\n")
        assert problems == [], line  # the shape alone is fine: CI cannot see the record
        found = check.compare_record(parsed, path, tree)
        assert found and "prove.py recorded" in found[0], (line, found)


def test_the_recorded_line_passes_in_any_of_its_spellings(tmp_path: Path) -> None:
    path, tree, line = _proven_record(tmp_path)
    for variant in (
        line,
        f"- {line}",
        f"1. {line}",
        f"`{line}`",
        line + ".",
        "\t" + line.replace(" ", "  "),
    ):
        problems, parsed = check.check("fix: x", f"## Tests\r\n\r\n{variant}\r\n")
        assert problems == [], variant
        assert check.compare_record(parsed, path, tree) == [], variant


def test_a_not_provable_line_is_compared_with_the_recorded_reason(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\ndef test_smoke():\n    assert last_char('abc') == 'b'\n\n\n"
        "def test_more():\n    assert last_char('xy') == 'x'\n"
    )
    _git(repo, "commit", "-aqm", "test only")
    assert _run(repo).returncode == 10
    path = str(Path(_git(repo, "rev-parse", "--absolute-git-dir")) / prove.RECORD_FILENAME)
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    _, good = check.check("fix: x", "## Tests\n\nProof: not provable -- test-only change\n")
    assert check.compare_record(good, path, tree) == []
    _, em = check.check("fix: x", "## Tests\n\nProof: not provable \u2014 test-only change.\n")
    assert check.compare_record(em, path, tree) == []
    _, other = check.check("fix: x", "## Tests\n\nProof: not provable -- copy-only change\n")
    assert check.compare_record(other, path, tree)


def test_a_not_proven_record_refuses_every_line(tmp_path: Path) -> None:
    """Tests that pass with the fix reverted leave no honest line, "not provable" included."""
    record = tmp_path / "rec.json"
    for verdict in ("NOT_PROVEN", "INCONCLUSIVE", "BASELINE_RED"):
        record.write_text(
            json.dumps({"tree": "t", "head": "a" * 40, "verdict": verdict, "line": None}),
            encoding="utf-8",
        )
        for line in (
            "Proof: not provable -- test-only change",
            "Proof: PROVEN test_a.py::t (head " + "a" * 12 + ")",
        ):
            _, parsed = check.check("fix: x", f"## Tests\n\n{line}\n")
            found = check.compare_record(parsed, str(record), "t")
            assert found and verdict in found[0], (verdict, line, found)


def test_another_tree_or_no_record_leaves_a_not_provable_line_alone(tmp_path: Path) -> None:
    path, _, _ = _proven_record(tmp_path)
    _, parsed = check.check("fix: x", "## Tests\n\nProof: not provable -- test-only change\n")
    assert check.compare_record(parsed, path, "some other tree") == []  # CI's shape-only case
    assert check.compare_record(parsed, str(tmp_path / "none.json"), "t") == []


def test_a_missing_record_fails_the_comparison(tmp_path: Path) -> None:
    _, proven = check.check(
        "fix: x", "## Tests\n\nProof: PROVEN test_a.py::t (head abcdef012345)\n"
    )
    problems = check.compare_record(proven, str(tmp_path / "nope.json"), "tree")
    assert problems and "no prove.py record" in problems[0]


def test_a_not_proven_run_prints_no_line_and_records_the_verdict(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "mod.py").write_text("def last_char(s):\n    return s[len(s) - 1]\n")
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\ndef test_a_str():\n    assert isinstance(last_char('abc'), str)\n"
    )
    _git(repo, "commit", "-aqm", "fix with a weak test")
    r = _run(repo)
    assert r.returncode == 20
    assert "Proof:" not in r.stdout
    record = _record(repo)
    assert record["verdict"] == "NOT_PROVEN" and record["line"] is None
    # a PROVEN line pasted anyway is refused by the comparison
    gitdir = _git(repo, "rev-parse", "--absolute-git-dir")
    _, proven = check.check(
        "fix: x", "## Tests\n\nProof: PROVEN test_mod.py::t (head abcdef012345)\n"
    )
    problems = check.compare_record(
        proven, str(Path(gitdir) / prove.RECORD_FILENAME), _git(repo, "rev-parse", "HEAD^{tree}")
    )
    assert problems and "NOT_PROVEN" in problems[0]


# ------------------------------------------------- not provable (closed list)


def test_frontend_tests_exit_eleven_and_print_the_frontend_reason(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    web = repo / "website" / "src"
    web.mkdir(parents=True)
    (web / "thing.ts").write_text("export const n = 1;\n")
    (web / "thing.test.ts").write_text("import { n } from './thing';\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fix(ui): thing")
    r = _run(repo)
    assert r.returncode == 11, r.stdout + r.stderr
    assert r.stdout.rstrip().splitlines()[-1] == (
        "Proof: not provable -- frontend test (prove.py runs Python only)"
    )
    assert _record(repo)["verdict"] == "FRONTEND_TESTS"


# ------------------------------------------------ git isolation (in-process and subprocess)


def test_the_autouse_fixture_drops_location_variables_and_keeps_the_config_pins(
    _floor_monkeypatch,
) -> None:
    _floor_monkeypatch.setenv("GIT_DIR", "/nonexistent")
    _floor_monkeypatch.setenv("GIT_PREFIX", "sub/")
    assert prove._git_env().get("GIT_DIR") is None  # the helper scrubs what the fixture did not
    # the fixture ran before this body: the pins from conftest are still pointing at /dev/null
    assert os.environ["GIT_CONFIG_GLOBAL"] == os.devnull
    assert os.environ["GIT_CONFIG_SYSTEM"] == os.devnull
    assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
    assert os.environ.get("GIT_AUTHOR_NAME")  # conftest's identity pin is not deleted either


def test_prove_git_env_drops_location_and_pins_hooks_off(_floor_monkeypatch) -> None:
    for key in _GIT_LOCATION_VARS | {"GIT_PREFIX"}:
        _floor_monkeypatch.setenv(key, "/elsewhere")
    _floor_monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.hooksPath'='/elsewhere'")
    env = prove._git_env()
    assert not (set(env) & (_GIT_LOCATION_VARS | {"GIT_PREFIX"}))
    assert "GIT_CONFIG_PARAMETERS" not in env  # a parent's `git -c key=value` is not inherited
    # the pins are argv options, the one source nothing inherited can outrank
    assert prove._git_argv(["status"]) == [
        "git",
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "core.fsmonitor=false",
        "status",
    ]


def test_prove_git_env_keeps_the_callers_other_config(_floor_monkeypatch) -> None:
    _floor_monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    _floor_monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    _floor_monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/x")
    env = prove._git_env()
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("safe.directory", "/x")


def test_every_git_subprocess_in_prove_goes_through_the_pinned_argv() -> None:
    """Class sweep: no `["git", ...]` literal or bare `subprocess` git call bypasses the pins."""
    source = PROVE.read_text(encoding="utf-8")
    assert '["git"' not in source.replace('return ["git", *GIT_PINS, *args]', "")
    assert source.count("_git_argv(") >= 3  # _git plus the two `git apply` calls
    assert "GIT_CONFIG_PARAMETERS" in source


def test_a_hooks_path_inherited_through_git_config_parameters_does_not_run(tmp_path: Path) -> None:
    """What `git -c core.hooksPath=<dir> push` exports to a hook's children."""
    repo = _repo(tmp_path)
    hooks = tmp_path / "param-hooks"
    hooks.mkdir()
    marker = tmp_path / "param-hook-ran"
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker.as_posix()}'\n", encoding="utf-8")
    hook.chmod(0o755)
    _fix_with_control_test(repo)
    env = {**_env(), "GIT_CONFIG_PARAMETERS": f"'core.hooksPath'='{hooks.as_posix()}'"}
    r = subprocess.run(
        [sys.executable, "-B", str(PROVE), "--base", "main"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert not marker.exists(), "an inherited GIT_CONFIG_PARAMETERS hooks path ran a hook"


def test_the_inherited_hooks_path_would_have_run_without_the_pins(tmp_path: Path) -> None:
    """Control: the same setup does run the hook for a plain `git worktree add`."""
    repo = _repo(tmp_path)
    hooks = tmp_path / "param-hooks"
    hooks.mkdir()
    marker = tmp_path / "control-hook-ran"
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker.as_posix()}'\n", encoding="utf-8")
    hook.chmod(0o755)
    env = {**_env(), "GIT_CONFIG_PARAMETERS": f"'core.hooksPath'='{hooks.as_posix()}'"}
    subprocess.run(
        ["git", "worktree", "add", "-q", "--detach", str(tmp_path / "wt"), "HEAD"],
        cwd=str(repo),
        env=env,
        check=True,
        capture_output=True,
    )
    if not marker.exists():
        pytest.skip("this git or shell does not run post-checkout hooks here")


def test_a_post_checkout_hook_does_not_run_in_the_proof_worktree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker = tmp_path / "hook-ran"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker.as_posix()}'\n", encoding="utf-8")
    hook.chmod(0o755)
    _fix_with_control_test(repo)
    r = _run(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not marker.exists(), "git worktree add ran the repository's post-checkout hook"


def test_a_global_hooks_path_does_not_run_either(tmp_path: Path) -> None:
    """The reviewer's case: the developer's own config names a hooks directory."""
    repo = _repo(tmp_path)
    hooks = tmp_path / "global-hooks"
    hooks.mkdir()
    marker = tmp_path / "global-hook-ran"
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker.as_posix()}'\n", encoding="utf-8")
    hook.chmod(0o755)
    config = tmp_path / "gitconfig"
    # Forward slashes: git reads a backslash in a config value as an escape, so a Windows
    # path written raw ("C:\Users\...") is a "bad config line" and every git call fails.
    config.write_text(f"[core]\n\thooksPath = {hooks.as_posix()}\n", encoding="utf-8")
    _fix_with_control_test(repo)
    env = {**_env(), "GIT_CONFIG_GLOBAL": str(config)}
    r = subprocess.run(
        [sys.executable, "-B", str(PROVE), "--base", "main"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert not marker.exists()


def test_an_exported_git_dir_does_not_retarget_prove(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    repo = _repo(tmp_path)
    _fix_with_control_test(repo)
    env = {**_env(), "GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}
    r = subprocess.run(
        [sys.executable, "-B", str(PROVE), "--base", "main"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert _record(repo)["tree"] == _git(repo, "rev-parse", "HEAD^{tree}")


def test_a_backend_fix_with_a_frontend_test_is_not_called_frontend_only(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "mod.py").write_text("def last_char(s):\n    return s[len(s) - 1]\n")
    web = repo / "website" / "src"
    web.mkdir(parents=True)
    (web / "thing.test.ts").write_text("import { n } from './thing';\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fix: backend, with a frontend test")
    r = _run(repo)
    assert r.returncode == 10, r.stdout + r.stderr
    assert "Proof:" not in r.stdout  # the author writes other: <20+ characters>
    assert _record(repo)["verdict"] == "NOTHING_TO_PROVE"


def test_frontend_only_means_every_production_path_is_under_website() -> None:
    only = prove.is_frontend_only
    assert only(["website/src/a.ts", "website/src/a.test.ts"])
    assert only(["website/src/a.test.ts", "website/package.json", "docs/x.md"])
    assert only(["website/src/a.test.ts"])
    assert not only(["src/kiro_crew/x.py", "website/src/a.test.ts"])
    assert not only(["website/src/a.ts", "website/src/a.test.ts", "scripts/build.sh"])
    assert not only(["src/kiro_crew/x.py", "website/src/a.ts", "website/src/a.test.ts"])


def test_test_only_and_docs_only_changes_print_their_reason(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\ndef test_smoke():\n    assert last_char('abc') == 'b'\n\n\n"
        "def test_more():\n    assert last_char('xy') == 'x'\n"
    )
    _git(repo, "commit", "-aqm", "test only")
    r = _run(repo)
    assert r.returncode == 10
    assert r.stdout.rstrip().splitlines()[-1] == "Proof: not provable -- test-only change"

    (tmp_path / "docs").mkdir()
    docs = _repo(tmp_path / "docs")
    (docs / "NOTES.md").write_text("hello\n")
    _git(docs, "add", "-A")
    _git(docs, "commit", "-qm", "docs")
    r = _run(docs)
    assert r.returncode == 10
    assert r.stdout.rstrip().splitlines()[-1] == "Proof: not provable -- docs or config only"


def test_code_with_no_test_prints_no_line(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "mod.py").write_text("def last_char(s):\n    return s[len(s) - 1]\n")
    _git(repo, "commit", "-aqm", "fix with no test")
    r = _run(repo)
    assert r.returncode == 10
    assert "Proof:" not in r.stdout


def test_every_reason_prove_prints_is_in_the_checks_closed_list() -> None:
    for reason in (
        prove.REASON_FRONTEND,
        prove.REASON_TEST_ONLY,
        prove.REASON_DOCS_OR_CONFIG,
        prove.REASON_TIMED_OUT,
    ):
        assert check.reason_ok(reason), reason
        problems, _ = check.check("fix: x", f"## Tests\n\nProof: not provable -- {reason}\n")
        assert problems == []


def test_the_frontend_test_shape_is_narrow() -> None:
    assert prove.is_frontend_test_path("website/src/a/b.test.tsx")
    assert prove.is_frontend_test_path("website/src/a/__tests__/b.ts")
    assert not prove.is_frontend_test_path("website/src/a/b.tsx")
    assert not prove.is_frontend_test_path("src/kiro_crew/x.test.ts")


def test_the_line_names_at_most_five_tests_and_the_record_keeps_all() -> None:
    ids = [f"test_a.py::t{i}" for i in range(7)]
    line = prove.proof_line({"verdict": "PROVEN", "named": ids}, "a" * 40)
    assert line == "Proof: PROVEN " + ", ".join(ids[:5]) + " (head " + "a" * 12 + ")"
    assert prove.proof_line({"verdict": "PROVEN", "named": []}, "a" * 40) is None
    assert prove.proof_line({"verdict": "NOT_PROVEN"}, "a" * 40) is None


# ------------------------------------------- the proof is of the commit it started on


def test_a_head_that_moves_during_the_run_does_not_change_what_was_proven(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The author commits while pytest runs: the line and record keep the pinned ids."""
    repo = _repo(tmp_path)
    _fix_with_control_test(repo)
    pinned_commit = _git(repo, "rev-parse", "HEAD")
    pinned_tree = _git(repo, "rev-parse", "HEAD^{tree}")

    real_run_pytest = prove.run_pytest
    moved: list[str] = []

    def commit_then_run(worktree: Path, test_files: list[str], report: Path):
        if not moved:  # during the first (baseline) run
            (repo / "later.py").write_text("x = 1\n")
            _git(repo, "add", "later.py")
            _git(repo, "commit", "-qm", "work done while the proof ran")
            moved.append(_git(repo, "rev-parse", "HEAD"))
        return real_run_pytest(worktree, test_files, report)

    monkeypatch.setattr(prove, "run_pytest", commit_then_run)
    monkeypatch.setattr(sys, "argv", ["prove.py", "--base", "main"])
    monkeypatch.chdir(repo)
    assert prove.main() == 0
    out = capsys.readouterr().out

    assert moved and moved[0] != pinned_commit
    assert _git(repo, "rev-parse", "HEAD") == moved[0]  # HEAD did move
    record = _record(repo)
    assert record["head"] == pinned_commit
    assert record["tree"] == pinned_tree
    assert record["tree"] != _git(repo, "rev-parse", "HEAD^{tree}")
    assert f"(head {pinned_commit[:12]})" in out
    assert moved[0][:12] not in out
    # the later tree is correctly stale against this record
    _, proven = check.check("fix: x", f"## Tests\n\n{record['line']}\n")
    gitdir = _git(repo, "rev-parse", "--absolute-git-dir")
    path = str(Path(gitdir) / prove.RECORD_FILENAME)
    assert check.compare_record(proven, path, pinned_tree) == []
    assert check.compare_record(proven, path, _git(repo, "rev-parse", "HEAD^{tree}"))


def test_the_proof_runs_on_the_pinned_commit_not_on_head(tmp_path: Path) -> None:
    """A later commit that breaks the fix must not turn a proven fix into a failure."""
    repo = _repo(tmp_path)
    _fix_with_control_test(repo)
    commit, _ = prove.pin_commit(repo)
    (repo / "mod.py").write_text("def last_char(s):\n    return 'broken'\n")
    _git(repo, "commit", "-aqm", "later work")
    result = prove.prove(repo, "main", False, commit)
    assert result["verdict"] == "PROVEN"
    assert result["named"] == ["test_mod.py::test_last"]


# ------------------------------------------- ids that look like HTML comments


def test_a_test_id_with_a_comment_marker_survives_the_body(tmp_path: Path) -> None:
    ids = ["test_x[<!--kirocrew:docked height=180-->-180]", "test_y"]
    line = prove.proof_line({"verdict": "PROVEN", "named": ids}, "a" * 40)
    assert "<!--" not in line and "<&#33;--" in line
    problems, proven = check.check("fix: x", f"## Tests\n\n{line}\n")
    assert problems == [] and proven is not None
    record = tmp_path / "rec.json"
    record.write_text(
        json.dumps({"tree": "t", "head": "a" * 40, "verdict": "PROVEN", "tests": ids}),
        encoding="utf-8",
    )
    assert check.compare_record(proven, str(record), "t") == []
    # a name the record does not hold is still refused
    bad = line.replace("test_y", "test_z")
    _, proven_bad = check.check("fix: x", f"## Tests\n\n{bad}\n")
    assert check.compare_record(proven_bad, str(record), "t")
