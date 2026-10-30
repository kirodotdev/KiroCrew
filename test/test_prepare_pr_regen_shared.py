"""``regen_shared.py``: rebuild shared generated files last, on a fresh rebase.

The generator table is data, so the engine is tested with stub generators in a
throwaway git repository, and the real table is checked against this checkout:
every file and tool it names exists, and the files it does not cover have no
generator.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from git_maintenance_helpers import git_maintenance_off_entries
from skill_script_helpers import load_skill_script

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

ROOT = Path(__file__).resolve().parent.parent
PREPARE_PR = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "kirocrew-prepare-pr"
SCRIPT = PREPARE_PR / "scripts" / "regen_shared.py"
SKILL = PREPARE_PR / "SKILL.md"
REFERENCE = PREPARE_PR / "references" / "split-and-regen.md"

rs = load_skill_script("prepare_pr_regen_shared_under_test", SCRIPT)


def _env() -> dict[str, str]:
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


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=_env())


@pytest.fixture(autouse=True)
def _git_identity(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """The script shells out to git with the ambient environment."""
    for key, value in _env().items():
        _floor_monkeypatch.setenv(key, value)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _entry(
    files: list[str],
    code: str,
    *,
    requires: list[str] | None = None,
    inputs: list[str] | None = None,
    name: str = "stub",
) -> dict:
    return {
        "name": name,
        "files": files,
        "requires": requires or [],
        "inputs": inputs or [],
        "argv": [sys.executable, "-c", code],
        "note": "test stub",
    }


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """``origin/main`` is one commit; HEAD adds a commit that edits nothing shared."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _write(root / "gen" / "out.txt", "stale\n")
    _write(root / "src.txt", "1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    _write(root / "src.txt", "2\n")
    _git(root, "commit", "-q", "-am", "mine")
    return root


REWRITE = "open('gen/out.txt','w').write('fresh\\n')"
NOOP = "pass"


def _touch_generated(repo: Path) -> None:
    _write(repo / "gen" / "out.txt", "branch edit\n")
    _git(repo, "commit", "-q", "-am", "touch generated file")


def test_nothing_stale_runs_no_generator_and_exits_zero(repo: Path) -> None:
    marker = repo / "ran"
    code, lines = rs.regen(
        str(repo), "main", [_entry(["gen/out.txt"], f"open({str(marker)!r},'w').write('x')")]
    )
    assert code == 0
    assert not marker.exists()
    assert "nothing to do" in lines[-1]


def test_a_stale_file_is_regenerated_and_exit_is_twenty(repo: Path) -> None:
    _touch_generated(repo)
    code, lines = rs.regen(str(repo), "main", [_entry(["gen/out.txt"], REWRITE)])
    assert code == 20
    assert (repo / "gen" / "out.txt").read_text(encoding="utf-8") == "fresh\n"
    assert "  rewrote: gen/out.txt" in lines
    hint = shlex.split(lines[-1].removeprefix("amend what changed: "))
    assert hint[0] == "python3"
    assert hint[2:] == ["--amend", "--", "gen/out.txt"]
    # Run from the repository root, the script must be reachable by the printed path.
    assert os.path.isabs(hint[1])
    assert Path(hint[1]).is_file()
    assert Path(hint[1]) == PREPARE_PR / "scripts" / "push_guard.py"


def test_the_amend_hint_quotes_a_path_with_spaces(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _touch_generated(repo)
    monkeypatch.setattr(rs, "PUSH_GUARD", "/tmp/my skill/scripts/push_guard.py")
    _, lines = rs.regen(str(repo), "main", [_entry(["gen/out.txt"], REWRITE)])
    assert shlex.split(lines[-1].removeprefix("amend what changed: "))[1] == (
        "/tmp/my skill/scripts/push_guard.py"
    )


def test_a_generator_that_changes_nothing_is_exit_zero(repo: Path) -> None:
    _touch_generated(repo)
    code, lines = rs.regen(str(repo), "main", [_entry(["gen/out.txt"], NOOP)])
    assert code == 0
    assert "  already current" in lines


def test_a_file_the_base_moved_also_counts(repo: Path) -> None:
    """The branch has not touched the file, but the base did since the merge base."""
    _git(repo, "checkout", "-q", "-b", "upstream", "origin/main")
    _write(repo / "gen" / "out.txt", "base moved\n")
    _git(repo, "commit", "-q", "-am", "base edits generated file")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "main")
    code, _ = rs.regen(str(repo), "main", [_entry(["gen/out.txt"], REWRITE)])
    assert code == 20


def test_an_uncommitted_edit_counts_as_the_branchs_own(repo: Path) -> None:
    _write(repo / "gen" / "out.txt", "edited, not committed\n")
    code, _ = rs.regen(str(repo), "main", [_entry(["gen/out.txt"], REWRITE)])
    assert code == 20


def test_a_changed_generator_input_makes_the_output_stale(repo: Path) -> None:
    """``src.txt`` changed on the branch; the generated file itself did not."""
    entry = _entry(["gen/out.txt"], REWRITE, inputs=["src.*"])
    code, lines = rs.regen(str(repo), "main", [entry])
    assert code == 20
    assert "  rewrote: gen/out.txt" in lines


def test_an_input_pattern_crosses_directories_and_misses_unrelated_paths(repo: Path) -> None:
    entries = [_entry(["gen/out.txt"], REWRITE, inputs=["site/src/*"])]
    assert rs.stale_entries(entries, ["site/src/a/b/c.ts"], []) == entries
    assert rs.stale_entries(entries, ["site/other.ts", "src.txt"], []) == []
    assert rs.stale_entries(entries, [], ["site/src/x.ts"]) == entries


def test_taking_the_base_side_of_a_conflicted_output_is_still_stale(tmp_path: Path) -> None:
    """The recipe in split-and-regen.md: ``git checkout --ours`` on the generated file.

    Both sides edit the generated file and the branch also edits an input. After the
    base side wins, the output equals base and is absent from the branch's diff, so
    only the input says it is stale.
    """
    root = tmp_path / "conflict"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _write(root / "gen" / "out.txt", "generated from 1\n")
    _write(root / "src.txt", "1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    _write(root / "src.txt", "2\n")
    _write(root / "gen" / "out.txt", "generated from 2\n")
    _git(root, "commit", "-q", "-am", "branch edits input and output")
    _git(root, "checkout", "-q", "main")
    _write(root / "gen" / "out.txt", "generated from 1, plus an upstream row\n")
    _git(root, "commit", "-q", "-am", "upstream edits the output")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(root, "checkout", "-q", "feature")

    stopped = subprocess.run(
        ["git", "rebase", "refs/remotes/origin/main"],
        cwd=root,
        capture_output=True,
        env=_env(),
    )
    assert stopped.returncode != 0, "the rebase must stop on the generated file"
    _git(root, "checkout", "--ours", "--", "gen/out.txt")
    _git(root, "add", "gen/out.txt")
    subprocess.run(
        ["git", "rebase", "--continue"],
        cwd=root,
        check=True,
        capture_output=True,
        env={**_env(), "GIT_EDITOR": "true"},
    )

    changed = subprocess.run(
        ["git", "diff", "--name-only", "refs/remotes/origin/main...HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(),
        check=True,
    ).stdout.split()
    assert changed == ["src.txt"], "the output now equals the base side"

    outputs_only = _entry(["gen/out.txt"], REWRITE)
    code, lines = rs.regen(str(root), "main", [outputs_only])
    assert code == 0, "an outputs-only trigger reads this as nothing to do"
    assert "nothing to do" in lines[-1]

    with_inputs = _entry(["gen/out.txt"], REWRITE, inputs=["src.txt"])
    code, lines = rs.regen(str(root), "main", [with_inputs])
    assert code == 20
    assert (root / "gen" / "out.txt").read_text(encoding="utf-8") == "fresh\n"
    assert "  rewrote: gen/out.txt" in lines


def test_a_generator_that_touches_another_file_is_an_error(repo: Path) -> None:
    _touch_generated(repo)
    code, lines = rs.regen(
        str(repo),
        "main",
        [_entry(["gen/out.txt"], REWRITE + ";open('src.txt','w').write('oops\\n')")],
    )
    assert code == 2
    assert "outside its list: src.txt" in lines[-1]


def test_a_failing_generator_is_an_environment_error_with_its_tail(repo: Path) -> None:
    _touch_generated(repo)
    code, lines = rs.regen(
        str(repo),
        "main",
        [_entry(["gen/out.txt"], "import sys; print('boom'); sys.exit(3)")],
    )
    assert code == 2
    assert "exited 3" in lines[-1]
    assert "boom" in lines[-1]


def test_a_missing_tool_is_an_environment_error(repo: Path) -> None:
    _touch_generated(repo)
    entry = _entry(["gen/out.txt"], NOOP)
    entry["argv"] = ["definitely-not-a-tool-xyz", "run"]
    code, lines = rs.regen(str(repo), "main", [entry])
    assert code == 2
    assert "tool `definitely-not-a-tool-xyz` not found" in lines[-1]


def test_the_generator_is_launched_by_the_path_which_found(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no shell, Windows only starts `npm` through the full `npm.cmd` path."""
    _touch_generated(repo)
    found = str(repo / "bin" / "npm.CMD")
    launched: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(args, *rest, **kwargs):
        if args and args[0] == found:
            launched.append(list(args))
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(rs.shutil, "which", lambda name: found if name == "npm" else None)
    monkeypatch.setattr(rs.subprocess, "run", fake_run)
    entry = _entry(["gen/out.txt"], NOOP)
    entry["argv"] = ["npm", "--prefix", "website", "run", "gen:ui"]
    code, lines = rs.regen(str(repo), "main", [entry])
    assert code == 0, lines
    assert launched == [[found, "--prefix", "website", "run", "gen:ui"]]


def test_an_entry_whose_generator_is_absent_is_skipped(repo: Path) -> None:
    _touch_generated(repo)
    entry = _entry(["gen/out.txt"], REWRITE, requires=["scripts/not_here.py"])
    code, lines = rs.regen(str(repo), "main", [entry])
    assert code == 0
    assert lines[0] == "skip stub: scripts/not_here.py not in this repository"
    assert (repo / "gen" / "out.txt").read_text(encoding="utf-8") == "branch edit\n"


def test_a_touched_hand_edited_file_prints_a_reminder_and_changes_nothing(repo: Path) -> None:
    _write(repo / "docs" / "feature-map" / "README.md", "row\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "map row")
    code, lines = rs.regen(str(repo), "main", [])
    assert code == 0
    assert any(
        line.startswith("HAND: docs/feature-map/README.md has no generator") for line in lines
    )


def test_missing_base_and_non_repo_are_environment_errors(tmp_path: Path, repo: Path) -> None:
    assert rs.regen(str(repo), "nope", [])[0] == 2
    plain = tmp_path / "plain"
    plain.mkdir()
    assert rs.regen(str(plain), "main", [])[0] == 2


def _cli(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(),
    )


def test_cli_list_prints_the_table_and_the_uncovered_files(repo: Path) -> None:
    out = _cli(repo, "--list")
    assert out.returncode == 0
    assert "file-length baseline" in out.stdout
    assert "Not covered (no generator; edit by hand):" in out.stdout
    assert "docs/feature-map/README.md" in out.stdout


def test_cli_rejects_an_unknown_flag(repo: Path) -> None:
    assert _cli(repo, "--rewrite-everything").returncode == 2


def test_cli_on_a_repo_without_the_real_generators_does_nothing(repo: Path) -> None:
    out = _cli(repo)
    assert out.returncode == 0
    assert out.stdout.count("skip ") == len(rs.REGEN)


# ------------------------------------------------- the real table, this checkout


@pytest.mark.parametrize("entry", rs.REGEN, ids=lambda e: e["name"])
def test_every_real_entry_names_files_and_a_generator_that_exist(entry: dict) -> None:
    for path in entry["files"] + entry["requires"]:
        assert (ROOT / path).exists(), path
    assert entry["argv"][0] in {"python3", "npm"}


def test_every_real_entry_reruns_on_the_changes_its_generator_reads(tmp_path: Path) -> None:
    by_name = {e["name"]: e for e in rs.REGEN}
    ui = by_name["find_ui index"]
    for path in ("website/src/uiLocations/areas/x.ts", "website/scripts/lib/ui-index.mjs"):
        assert rs.stale_entries([ui], [path], [], str(ROOT)) == [ui]
    assert rs.stale_entries([ui], ["src/kiro_crew/chat.py"], [], str(ROOT)) == []
    # The file-length baseline only shrinks, and it is empty once every file is under the
    # cap, so its row check reads a one-row copy at the same path, in the shape the real
    # file has while a file is still over.
    capped = tmp_path / by_name["file-length baseline"]["files"][0]
    capped.parent.mkdir(parents=True)
    capped.write_text("2001 src/kiro_crew/over_the_cap.py\n", encoding="utf-8")
    for name, root in (("file-length baseline", tmp_path), ("black baseline", ROOT)):
        entry = by_name[name]
        assert entry["listed_in_output"] is True
        listed = sorted(rs.baseline_paths(str(root), entry["files"][0]))
        assert listed, name
        assert rs.stale_entries([entry], [listed[0]], [], str(root)) == [entry]
        assert (
            rs.stale_entries([entry], ["src/kiro_crew/not_in_a_baseline.py"], [], str(root)) == []
        )


def test_a_baseline_entry_is_stale_when_a_changed_path_is_one_of_its_rows(repo: Path) -> None:
    _write(repo / "gen" / "base.txt", "# rows\n12 old/path.py\nold/other.py\n")
    entry = _entry(["gen/base.txt"], NOOP)
    entry["listed_in_output"] = True
    assert rs.stale_entries([entry], ["old/path.py"], [], str(repo)) == [entry]
    assert rs.stale_entries([entry], ["old/other.py"], [], str(repo)) == [entry]
    assert rs.stale_entries([entry], ["new/path.py", "# rows"], [], str(repo)) == []


def test_a_rename_reports_both_paths_so_a_row_for_the_old_one_is_found(repo: Path) -> None:
    _git(repo, "mv", "src.txt", "moved.txt")
    _git(repo, "commit", "-q", "-m", "rename")
    _write(repo / "gen" / "base.txt", "src.txt\n")
    entry = _entry(["gen/base.txt"], "open('gen/base.txt','w').write('pruned\\n')")
    entry["listed_in_output"] = True
    code, _ = rs.regen(str(repo), "main", [entry])
    assert code == 20


def test_the_baseline_generators_accept_the_flag_the_table_passes() -> None:
    for entry in rs.REGEN:
        if entry["argv"][0] != "python3":
            continue
        assert entry["argv"][2] == "--update-baseline"
        assert "--update-baseline" in (ROOT / entry["argv"][1]).read_text(encoding="utf-8")


def test_the_ui_index_generator_is_the_npm_script_that_writes_both_files() -> None:
    package = (ROOT / "website" / "package.json").read_text(encoding="utf-8")
    assert '"gen:ui": "node scripts/gen-ui-index.mjs"' in package
    entry = next(e for e in rs.REGEN if e["argv"][-1] == "gen:ui")
    assert entry["argv"] == ["npm", "--prefix", "website", "run", "gen:ui"]
    assert len(entry["files"]) == 2


def test_no_covered_file_is_also_listed_as_uncovered() -> None:
    covered = {f for e in rs.REGEN for f in e["files"]}
    assert not covered & set(rs.NOT_COVERED)


def test_every_uncovered_file_exists_so_the_reminder_names_a_real_path() -> None:
    for path in rs.NOT_COVERED:
        assert (ROOT / path).is_file(), path


def test_the_skill_names_the_script_and_the_reference_it_points_to_exists() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    assert "`regen_shared.py [base]`" in skill
    assert "references/split-and-regen.md" in skill
    reference = REFERENCE.read_text(encoding="utf-8")
    for entry in rs.REGEN:
        for path in entry["files"]:
            assert path in reference, path
    for path in rs.NOT_COVERED:
        assert path in reference, path
