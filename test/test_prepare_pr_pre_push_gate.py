"""``pre_push_gate.py``: the optional, advisory pre-push gate runner (RFC decision D7).

The red path is a gate that fails or cannot run, and a ``Gate proof:`` line the
recorded proof contradicts. The green path is a proof written for the exact tree, a
skip that carries a code, and a body with no line at all, which never fails.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from git_maintenance_helpers import git_maintenance_off_entries
from skill_script_helpers import load_skill_script

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

ROOT = Path(__file__).resolve().parent.parent
PREPARE_PR = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "kirocrew-prepare-pr"
SCRIPT = PREPARE_PR / "scripts" / "pre_push_gate.py"

gate = load_skill_script("prepare_pr_pre_push_gate_under_test", SCRIPT)

OK_GATE = {"id": "ok", "cmd": ["{py}", "-c", "pass"], "ci": {"file": "ci.yml", "text": "x"}}
BAD_GATE = {
    "id": "bad",
    "cmd": ["{py}", "-c", "print('boom-one'); raise SystemExit(3)"],
    "ci": {"file": "ci.yml", "text": "x"},
}
BAD_GATE_2 = {
    "id": "bad2",
    "cmd": ["{py}", "-c", "print('boom-two'); raise SystemExit(4)"],
    "ci": {"file": "ci.yml", "text": "x"},
}
NO_TOOL_GATE = {
    "id": "needs-tool",
    "cmd": ["{py}", "-c", "raise SystemExit(9)"],
    "module": "no_such_module_for_the_gate_test",
    "ci": {"file": "ci.yml", "text": "x"},
}
BASE_GATE = {
    "id": "needs-base",
    "cmd": ["{py}", "-c", "import os, sys; sys.exit(0 if os.environ['X_BASE'] else 1)"],
    "base_env": "X_BASE",
    "ci": {"file": "ci.yml", "text": "x"},
}
GONE_GATE = {
    "id": "gone",
    "cmd": ["/no/such/executable/for/the/gate/test"],
    "ci": {"file": "ci.yml", "text": "x"},
}
OTHER_REASON = "other:the CI job needs a service this machine does not have"


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
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_git_env(),
    )
    return done.stdout.strip()


def _make_repo(parent: Path, name: str) -> Path:
    """A branch one commit past a synthetic ``origin/main``."""
    repo = parent / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "mine.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")
    return repo


def _enter(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    return repo


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A branch one commit past a synthetic ``origin/main``, with the cwd set to it."""
    return _enter(_make_repo(tmp_path, "repo"), monkeypatch)


@pytest.fixture
def use_gates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def _use(*gates: dict) -> None:
        path = tmp_path / "gate_map.json"
        path.write_text(json.dumps({"gates": list(gates), "ci_only": []}), encoding="utf-8")
        monkeypatch.setattr(gate, "MAP_PATH", str(path))

    return _use


def _proof(repo: Path) -> Path:
    return repo / ".git" / gate.PROOF_FILENAME


# ------------------------------------------------------------------ green path


def test_every_gate_green_writes_a_proof_for_this_tree(repo, use_gates, capsys) -> None:
    use_gates(OK_GATE)
    assert gate.main(["pre_push_gate.py"]) == 0
    proof = json.loads(_proof(repo).read_text(encoding="utf-8"))
    assert proof["tree"] == _git(repo, "rev-parse", "HEAD^{tree}")
    assert proof["ran"] == ["ok"]
    assert proof["skipped"] == []
    out = capsys.readouterr().out
    assert "Gate proof: tree {}; ran 1; skipped none".format(proof["tree"][:12]) in out


def test_a_missing_tool_is_skipped_with_its_code_and_the_proof_says_so(
    repo, use_gates, capsys
) -> None:
    use_gates(OK_GATE, NO_TOOL_GATE)
    assert gate.main(["pre_push_gate.py"]) == 0
    proof = json.loads(_proof(repo).read_text(encoding="utf-8"))
    assert proof["skipped"] == [
        {"gate": "needs-tool", "code": "tool-missing:no_such_module_for_the_gate_test"}
    ]
    assert "skipped needs-tool=tool-missing:no_such_module_for_the_gate_test" in (
        capsys.readouterr().out
    )


def test_a_skip_with_a_code_covers_a_gate_that_cannot_run(repo, use_gates) -> None:
    use_gates(OK_GATE, GONE_GATE)
    assert gate.main(["pre_push_gate.py", "--skip", "gone=no-network"]) == 0
    proof = json.loads(_proof(repo).read_text(encoding="utf-8"))
    assert proof["skipped"] == [{"gate": "gone", "code": "no-network"}]


def test_the_base_sha_reaches_a_gate_that_asks_for_it(repo, use_gates) -> None:
    use_gates(BASE_GATE)
    assert gate.main(["pre_push_gate.py"]) == 0


def test_a_gate_whose_paths_match_no_changed_file_does_not_run(repo, use_gates) -> None:
    only_docs = {**BAD_GATE, "id": "docs-only", "paths": [r"\.md$"]}
    use_gates(OK_GATE, only_docs)
    assert gate.main(["pre_push_gate.py"]) == 0
    assert json.loads(_proof(repo).read_text(encoding="utf-8"))["ran"] == ["ok"]


def test_the_self_test_runs_before_the_gate_and_a_red_one_fails_it(repo, use_gates) -> None:
    red_self_test = {**OK_GATE, "id": "st", "self_test": ["{py}", "-c", "raise SystemExit(1)"]}
    use_gates(red_self_test)
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_FAILED


def test_the_runner_self_test_passes(capsys) -> None:
    assert gate.main(["pre_push_gate.py", "--test"]) == 0
    assert "self-test: ok" in capsys.readouterr().out


# -------------------------------------------------------------------- red path


def test_every_failure_is_printed_at_once_and_exit_is_fifty(repo, use_gates, capsys) -> None:
    use_gates(OK_GATE, BAD_GATE, BAD_GATE_2)
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_FAILED == 50
    out = capsys.readouterr().out
    assert "FAIL  bad" in out and "boom-one" in out
    assert "FAIL  bad2" in out and "boom-two" in out
    assert not _proof(repo).exists(), "a red run records no proof"


def test_a_gate_that_cannot_start_and_has_no_skip_exits_fifty_one(repo, use_gates, capsys) -> None:
    use_gates(OK_GATE, GONE_GATE)
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_UNRUNNABLE == 51
    assert "CANNOT  gone" in capsys.readouterr().out
    assert not _proof(repo).exists()


def test_a_base_gate_without_a_merge_base_exits_fifty_one(
    repo, use_gates, capsys, monkeypatch
) -> None:
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    use_gates(BASE_GATE)
    assert gate.main(["pre_push_gate.py"]) == 51
    assert "CANNOT RUN  needs-base" in capsys.readouterr().out


def test_a_failed_gate_wins_over_an_unrunnable_one(repo, use_gates) -> None:
    use_gates(BAD_GATE, GONE_GATE)
    assert gate.main(["pre_push_gate.py"]) == 50


@pytest.mark.parametrize(
    "value",
    [
        "ok=nope",
        "ok=other:too short",
        "ok=tool-missing:",
        "ok=red-on-base:xyz",
        "ok=",
        "missing=no-network",
        "no-equals-sign",
    ],
)
def test_a_skip_without_a_valid_code_or_gate_is_refused(repo, use_gates, value) -> None:
    use_gates(OK_GATE)
    assert gate.main(["pre_push_gate.py", "--skip", value]) == gate.EXIT_ENV
    assert not _proof(repo).exists()


@pytest.mark.parametrize(
    "code",
    [
        "tool-missing:uv",
        "no-network",
        "unsupported-os",
        "red-on-base:0123abc",
        OTHER_REASON,
    ],
)
def test_every_documented_skip_code_is_accepted(code) -> None:
    assert gate.SKIP_CODE.match(code)


# ------------------------------------------------- what a proof may be about


def test_a_dirty_working_tree_refuses_the_run_and_writes_no_proof(repo, use_gates, capsys) -> None:
    use_gates(BAD_GATE)
    (repo / "mine.py").write_text("x = 2\n")  # an edit the gates would see and a tree would not
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_UNRUNNABLE
    out = capsys.readouterr().out
    assert "REFUSED: uncommitted changes" in out and "mine.py" in out
    assert "FAIL" not in out, "no gate runs against code the proof could not name"
    assert not _proof(repo).exists()


def test_a_new_untracked_file_also_refuses_the_run(repo, use_gates) -> None:
    use_gates(OK_GATE)
    (repo / "new.py").write_text("y = 1\n")
    assert gate.main(["pre_push_gate.py"]) == 51
    assert not _proof(repo).exists()


def test_a_clean_tree_after_the_edit_is_committed_gets_its_proof(repo, use_gates) -> None:
    use_gates(OK_GATE)
    (repo / "mine.py").write_text("x = 2\n")
    assert gate.main(["pre_push_gate.py"]) == 51
    _git(repo, "commit", "-qam", "edit")
    assert gate.main(["pre_push_gate.py"]) == 0
    assert json.loads(_proof(repo).read_text(encoding="utf-8"))["tree"] == _git(
        repo, "rev-parse", "HEAD^{tree}"
    )


def test_a_gate_that_leaves_a_file_behind_gets_no_proof(repo, use_gates, capsys) -> None:
    litter = {
        "id": "litter",
        "cmd": ["{py}", "-c", "open('litter.txt', 'w').write('x')"],
        "ci": {"file": "ci.yml", "text": "x"},
    }
    use_gates(litter)
    assert gate.main(["pre_push_gate.py"]) == 51
    assert "changed while the gates ran" in capsys.readouterr().out
    assert not _proof(repo).exists()


def test_a_gate_that_moves_head_gets_no_proof(repo, use_gates, capsys) -> None:
    mover = {
        "id": "mover",
        "cmd": [
            "git",
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "moved",
        ],
        "ci": {"file": "ci.yml", "text": "x"},
    }
    use_gates(mover)
    assert gate.main(["pre_push_gate.py"]) == 51
    assert "changed while the gates ran" in capsys.readouterr().out
    assert not _proof(repo).exists()


# ------------------------------------------------- no merge base: every gate runs


def test_without_a_merge_base_a_path_gated_gate_still_runs(repo, use_gates, capsys) -> None:
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    docs_only_red = {**BAD_GATE, "id": "docs-only", "paths": [r"\.md$"]}
    use_gates(OK_GATE, docs_only_red)
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_FAILED
    out = capsys.readouterr().out
    assert "running every gate" in out and "FAIL  docs-only" in out


def test_without_a_merge_base_a_passing_proof_lists_the_path_gated_gate(repo, use_gates) -> None:
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    gated = {**OK_GATE, "id": "gated", "paths": [r"\.md$"]}
    use_gates(OK_GATE, gated)
    assert gate.main(["pre_push_gate.py"]) == 0
    assert json.loads(_proof(repo).read_text(encoding="utf-8"))["ran"] == ["ok", "gated"]


def test_select_with_unknown_paths_chooses_every_gate_and_with_none_only_the_always_ones() -> None:
    gates = [{"id": "a"}, {"id": "b", "paths": [r"\.md$"]}]
    assert [g["id"] for g in gate.select(gates, None)] == ["a", "b"]
    assert [g["id"] for g in gate.select(gates, [])] == ["a"]


# ------------------------------------------------- the path filter on slow gates


def _selected(paths: list[str]) -> set[str]:
    return {g["id"] for g in gate.select(gate.load_map()["gates"], paths)}


def test_a_fixtures_only_change_runs_the_acp_frame_gate() -> None:
    assert "acp-frame-host-data" in _selected(["test/fixtures/acp_frames/kiro/session.jsonl"])


def test_a_change_to_nothing_a_gate_reads_still_runs_the_cheap_gates() -> None:
    chosen = _selected(["docs/guide.md"])
    assert {"black", "acp-frame-host-data", "comment-history"} <= chosen
    assert not {"isort", "flake8", "mypy", "subprocess-encoding"} & chosen


@pytest.mark.parametrize("config", ["pyproject.toml", "setup.cfg"])
def test_a_config_only_change_runs_the_lint_and_type_gates(config) -> None:
    assert {"isort", "flake8", "mypy"} <= _selected([config])


@pytest.mark.parametrize(
    ("changed", "gate_id"),
    [
        (".github/subprocess-encoding-baseline.txt", "subprocess-encoding"),
        (".github/agent-sdk-boundary-baseline.txt", "agent-sdk-boundary"),
        (".github/sync-io-in-async-baseline.txt", "sync-io-in-async"),
        (".github/sync-io-in-async-fs-baseline.txt", "sync-io-in-async"),
        ("scripts/check_sync_io_in_async.py", "sync-io-in-async"),
        ("scripts/ratchet_scope.py", "agent-sdk-boundary"),
        ("src/kiro_crew/chat.py", "mypy"),
        ("test/test_x.py", "flake8"),
        ("conftest.py", "isort"),
    ],
)
def test_a_change_to_an_input_of_a_slow_gate_runs_that_gate(changed, gate_id) -> None:
    assert gate_id in _selected([changed])


# --------------------------------- git output is parsed as bytes, never as quoted text

ODD_NAMES = [
    "src/kiro_crew/caf\u00e9.py",  # non-ASCII: git quotes it unless the listing is NUL-separated
    "src/kiro_crew/two words.py",  # a space
]
if sys.platform != "win32":
    ODD_NAMES.append("src/kiro_crew/new\nline.py")  # Windows cannot hold a newline in a name
SRC_ONLY = {
    "id": "src-only",
    "cmd": ["{py}", "-c", "raise SystemExit(3)"],
    "paths": ["^src/kiro_crew/"],
    "slow": "12.0 s",
    "ci": {"file": "ci.yml", "text": "x"},
}


def _commit_files(repo: Path, names: list[str]) -> None:
    for name in names:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("y = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "odd names")


def test_changed_lists_odd_names_exactly(repo) -> None:
    _commit_files(repo, ODD_NAMES)
    base = _git(repo, "merge-base", "HEAD", "origin/main")
    assert gate._changed(base) == sorted(["mine.py", *ODD_NAMES])


def test_a_rename_lists_the_old_and_the_new_path(repo) -> None:
    _commit_files(repo, ["src/kiro_crew/old name.py"])
    base = _git(repo, "merge-base", "HEAD", "origin/main")
    (repo / "test").mkdir()
    _git(repo, "mv", "src/kiro_crew/old name.py", "test/n\u00e9w name.py")
    _git(repo, "commit", "-qm", "move")
    assert gate._changed(base) == ["mine.py", "test/n\u00e9w name.py"]
    # A rename of a file the base already had lists both its old and its new path.
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    base2 = _git(repo, "merge-base", "HEAD", "origin/main")
    _git(repo, "mv", "mine.py", "src/kiro_crew/m\u00ebne.py")
    _git(repo, "commit", "-qm", "move again")
    assert gate._changed(base2) == ["mine.py", "src/kiro_crew/m\u00ebne.py"]


@pytest.mark.parametrize("name", ODD_NAMES)
def test_a_change_to_an_odd_name_still_runs_the_path_gated_gate(repo, use_gates, name) -> None:
    _commit_files(repo, [name])
    use_gates(SRC_ONLY)
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_FAILED, "the src/ filter missed the path"


def test_the_shipped_filters_select_a_non_ascii_source_path(repo) -> None:
    _commit_files(repo, ["src/kiro_crew/caf\u00e9.py"])
    base = _git(repo, "merge-base", "HEAD", "origin/main")
    chosen = {g["id"] for g in gate.select(gate.load_map()["gates"], gate._changed(base))}
    assert {"mypy", "flake8", "isort", "agent-sdk-boundary", "sync-io-in-async"} <= chosen


def test_a_dirty_odd_name_is_shown_unquoted(repo, use_gates, capsys) -> None:
    use_gates(OK_GATE)
    (repo / "caf\u00e9 au lait.py").write_text("z = 1\n", encoding="utf-8")
    assert gate.main(["pre_push_gate.py"]) == 51
    out = capsys.readouterr().out
    assert "caf\u00e9 au lait.py" in out and "\\303" not in out


def test_a_git_that_fails_is_an_error_not_a_clean_tree(repo, use_gates, capsys) -> None:
    use_gates(OK_GATE)
    (repo / ".git" / "index").write_bytes(b"not an index")
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_ENV
    assert "git status failed" in capsys.readouterr().err
    assert not _proof(repo).exists()


def test_a_repository_directory_with_spaces_and_non_ascii_gets_its_proof(
    tmp_path, monkeypatch, use_gates
) -> None:
    # Windows drops a trailing space from a directory name, so the space is only added elsewhere.
    name = "r\u00e9po with spaces" + ("" if sys.platform == "win32" else " ")
    odd = _enter(_make_repo(tmp_path, name), monkeypatch)
    use_gates(OK_GATE)
    assert gate.main(["pre_push_gate.py"]) == 0
    assert (odd / ".git" / gate.PROOF_FILENAME).is_file()
    assert gate.main(["pre_push_gate.py", "--verify"]) == 0


def test_running_from_a_subdirectory_still_sees_top_relative_paths(
    repo, use_gates, monkeypatch
) -> None:
    _commit_files(repo, ["src/kiro_crew/caf\u00e9.py"])
    (repo / "docs").mkdir()
    monkeypatch.chdir(repo / "docs")
    use_gates(SRC_ONLY)
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_FAILED


def test_git_paths_splits_on_nul_only(repo) -> None:
    _commit_files(repo, ODD_NAMES)
    assert sorted(gate.git_paths(["ls-files", "-z", "src"])) == sorted(ODD_NAMES)
    # git prints "C:/..." on Windows; compare as paths, not as strings.
    assert Path(gate.git_line(["rev-parse", "--show-toplevel"])) == repo


# ------------------------------------- git output does not depend on the user's git config


def _config(repo: Path, key: str, value: str) -> None:
    _git(repo, "config", key, value)


def test_untracked_files_are_seen_when_the_user_hides_them(repo, use_gates, capsys) -> None:
    """status.showUntrackedFiles=no must not let an untracked, imported file go unchecked."""
    use_gates(OK_GATE)
    _config(repo, "status.showUntrackedFiles", "no")
    (repo / "helper.py").write_text("z = 1\n", encoding="utf-8")
    assert gate.main(["pre_push_gate.py"]) == 51
    assert "helper.py" in capsys.readouterr().out
    assert not _proof(repo).exists()


def test_an_untracked_file_in_a_new_directory_is_listed_by_name(repo) -> None:
    """Without --untracked-files=all git shows the directory, not the file."""
    (repo / "pkg").mkdir()
    (repo / "pkg" / "inner.py").write_text("z = 1\n", encoding="utf-8")
    assert gate._worktree_state()[2] == ["?? pkg/inner.py"]


def test_a_changed_listing_ignores_diff_renames_and_color_config(repo) -> None:
    _commit_files(repo, ["src/kiro_crew/old.py"])
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    base = _git(repo, "merge-base", "HEAD", "origin/main")
    _git(repo, "mv", "src/kiro_crew/old.py", "src/kiro_crew/new.py")
    _git(repo, "commit", "-qm", "rename")
    for key, value in [
        ("diff.renames", "copies"),
        ("color.ui", "always"),
        ("diff.noprefix", "true"),
    ]:
        _config(repo, key, value)
    assert gate._changed(base) == ["src/kiro_crew/new.py", "src/kiro_crew/old.py"]
    _config(repo, "status.renames", "copies")
    (repo / "mine.py").write_text("x = 3\n", encoding="utf-8")
    assert gate._worktree_state()[2] == [" M mine.py"]


def test_a_command_line_config_from_an_ancestor_git_is_overridden(repo, monkeypatch) -> None:
    """GIT_CONFIG_PARAMETERS is what ``git -c`` leaves for the programs it starts."""
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'status.showUntrackedFiles=no'")
    (repo / "helper.py").write_text("z = 1\n", encoding="utf-8")
    assert gate._worktree_state()[2] == ["?? helper.py"]


def test_a_core_quotepath_setting_does_not_change_the_paths(repo) -> None:
    _commit_files(repo, ["src/kiro_crew/caf\u00e9.py"])
    base = _git(repo, "merge-base", "HEAD", "origin/main")
    for value in ("true", "false"):
        _config(repo, "core.quotepath", value)
        assert "src/kiro_crew/caf\u00e9.py" in gate._changed(base)


@pytest.mark.parametrize("var", ["GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE"])
def test_a_variable_that_retargets_git_is_not_inherited(repo, monkeypatch, var, tmp_path) -> None:
    """A pre-push hook runs with GIT_DIR and GIT_INDEX_FILE set; the proof is about this tree."""
    monkeypatch.setenv(var, str(tmp_path / "elsewhere"))
    assert gate._worktree_state()[2] == []
    assert gate.head_tree() == _git(repo, "rev-parse", "HEAD^{tree}")
    assert var not in gate.clean_environ()


def test_a_gate_does_not_inherit_a_retargeting_variable_either(
    repo, use_gates, monkeypatch
) -> None:
    sees_none = {
        "id": "env",
        "cmd": [
            "{py}",
            "-c",
            "import os, sys; sys.exit(1 if 'GIT_INDEX_FILE' in os.environ else 0)",
        ],
        "ci": {"file": "ci.yml", "text": "x"},
    }
    use_gates(sees_none)
    monkeypatch.setenv("GIT_INDEX_FILE", "elsewhere")
    assert gate.main(["pre_push_gate.py"]) == 0


def test_every_git_call_in_the_runner_goes_through_the_neutral_prefix() -> None:
    """A new ``["git", ...]`` literal would skip the overrides above; none may exist."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.count('"git",') == 1, "build git commands with git_argv()"


# ----------------------------------------------------------------------- verify


def test_verify_finds_no_proof_then_a_proof_then_none_after_the_tree_changes(
    repo, use_gates
) -> None:
    use_gates(OK_GATE)
    assert gate.main(["pre_push_gate.py", "--verify"]) == gate.EXIT_NO_PROOF == 52
    assert gate.main(["pre_push_gate.py"]) == 0
    assert gate.main(["pre_push_gate.py", "--verify"]) == 0
    (repo / "mine.py").write_text("x = 2\n")
    _git(repo, "commit", "-qam", "again")
    assert gate.main(["pre_push_gate.py", "--verify"]) == 52, "the proof names the old tree"


# ------------------------------------------------------- the Gate proof: line


TREE = "a" * 40


def _write_proof(git_dir: Path, **over) -> dict:
    proof = {
        "version": 1,
        "tree": TREE,
        "ran": ["x", "y"],
        "skipped": [{"gate": "z", "code": "no-network"}],
        **over,
    }
    (git_dir / gate.PROOF_FILENAME).write_text(json.dumps(proof), encoding="utf-8")
    return proof


def test_a_line_that_matches_the_proof_is_no_problem(tmp_path) -> None:
    proof = _write_proof(tmp_path)
    body = "## Tests\n\n{}\n".format(gate.proof_line(proof))
    assert gate.contradiction(body, str(tmp_path), TREE) == ""


def test_a_body_with_no_line_never_contradicts(tmp_path) -> None:
    assert gate.contradiction("## Tests\n\nran by hand\n", str(tmp_path), TREE) == ""
    _write_proof(tmp_path)
    assert gate.contradiction("## Tests\n\nran by hand\n", str(tmp_path), TREE) == ""


@pytest.mark.parametrize(
    "line",
    [
        "Gate proof: tree aaaaaaaaaaaa; ran 5; skipped z=no-network",
        "Gate proof: tree aaaaaaaaaaaa; ran 2; skipped none",
        "Gate proof: tree bbbbbbbbbbbb; ran 2; skipped z=no-network",
        "Gate proof: all green",
    ],
)
def test_a_line_the_proof_disagrees_with_is_reported(tmp_path, line) -> None:
    _write_proof(tmp_path)
    assert gate.contradiction(line, str(tmp_path), TREE)


def test_a_line_with_no_recorded_proof_is_reported(tmp_path) -> None:
    problem = gate.contradiction(
        "Gate proof: tree aaaaaaaaaaaa; ran 1; skipped none", str(tmp_path), TREE
    )
    assert "no recorded proof" in problem


@pytest.mark.parametrize("over", [{"tree": "b" * 40}, {"version": 2}, {"ran": "x"}])
def test_a_proof_for_another_tree_or_shape_is_no_proof(tmp_path, over) -> None:
    _write_proof(tmp_path, **over)
    assert gate.read_proof(str(tmp_path), TREE) is None


def test_a_corrupt_proof_file_is_no_proof(tmp_path) -> None:
    (tmp_path / gate.PROOF_FILENAME).write_text("{not json", encoding="utf-8")
    assert gate.read_proof(str(tmp_path), TREE) is None


# ------------------------------------------------------------------ the map


def test_the_shipped_map_loads_and_every_gate_names_what_it_runs() -> None:
    data = gate.load_map()
    assert len(data["gates"]) >= 20
    for entry in data["gates"]:
        assert entry["cmd"], entry["id"]


def test_a_map_with_a_repeated_id_is_refused(tmp_path) -> None:
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"gates": [OK_GATE, OK_GATE], "ci_only": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="listed twice"):
        gate.load_map(str(path))


def test_an_os_gate_off_its_platform_is_skipped_with_its_code(monkeypatch) -> None:
    monkeypatch.setattr(gate.sys, "platform", "win32")
    assert gate.unavailable({"os": ["linux", "darwin"]}) == "unsupported-os"
    assert gate.unavailable({}) == ""


def test_a_missing_executable_is_a_tool_missing_skip() -> None:
    assert gate.unavailable({"tool": "no-such-tool-for-the-gate-test"}) == (
        "tool-missing:no-such-tool-for-the-gate-test"
    )


# ------------------------------------------ hostile git config, run from a subdirectory
#
# The runner's answers must not depend on the user's git config or on the current directory.
# These are the ``diff.*``, ``status.*`` and ``core.*`` keys of ``git help --config`` that can
# change what a listing or a status prints or hides, each set to the value that does the most
# harm. ``diff.relative`` strips a subdirectory from every path; ``status.showUntrackedFiles``
# hides untracked files; ``core.ignoreStat`` hides edits; ``core.excludesFile`` (set in the test
# to a file that ignores everything) hides new files.

HOSTILE_GIT_CONFIG = {
    "diff.relative": "true",
    "diff.noPrefix": "true",
    "diff.mnemonicPrefix": "true",
    "diff.srcPrefix": "Y/",
    "diff.dstPrefix": "Z/",
    "diff.renames": "copies",
    "diff.renameLimit": "1",
    "diff.ignoreSubmodules": "all",
    "diff.submodule": "log",
    "diff.context": "0",
    "diff.interHunkContext": "9",
    "diff.algorithm": "patience",
    "diff.indentHeuristic": "false",
    "diff.autoRefreshIndex": "false",
    "diff.colorMoved": "zebra",
    "diff.suppressBlankEmpty": "true",
    "diff.statGraphWidth": "5",
    "diff.statNameWidth": "5",
    "diff.wsErrorHighlight": "all",
    "diff.external": "/no/such/diff/program",
    "diff.tool": "nonexistent",
    "status.relativePaths": "true",
    "status.showUntrackedFiles": "no",
    "status.renames": "copies",
    "status.renameLimit": "1",
    "status.short": "true",
    "status.branch": "true",
    "status.aheadBehind": "false",
    "status.showStash": "true",
    "status.submoduleSummary": "true",
    "status.displayCommentPrefix": "true",
    "core.quotePath": "true",
    "core.ignoreCase": "true",
    "core.autocrlf": "true",
    "core.safecrlf": "false",
    "core.fsmonitor": "/no/such/fsmonitor/hook",
    "core.untrackedCache": "true",
    "core.ignoreStat": "true",
    "core.fileMode": "false",
    "core.trustctime": "false",
    "core.checkStat": "minimal",
    "core.precomposeUnicode": "true",
    "core.preloadIndex": "false",
    "core.splitIndex": "true",
    "core.abbrev": "4",
    "core.commentChar": ";",
    "core.pager": "/no/such/pager",
    "color.ui": "always",
    "color.status": "always",
    "color.diff": "always",
}


def _hostile(repo: Path, tmp_path: Path, only: str | None = None) -> None:
    """Set the hostile config in the repository (one key, or all of them)."""
    ignore_all = tmp_path / "ignore-everything"
    ignore_all.write_text("*\n", encoding="utf-8")
    config = {**HOSTILE_GIT_CONFIG, "core.excludesFile": str(ignore_all)}
    for key, value in config.items():
        if only is None or key == only:
            _git(repo, "config", key, value)


@pytest.fixture
def branch_repo(repo: Path) -> Path:
    """Root, ``src/`` and ``website/`` files changed past ``origin/main``, one commit."""
    _commit_files(repo, ["src/kiro_crew/x.py", "website/y.ts", "docs/guide.md"])
    return repo


EXPECTED_CHANGED = sorted(["mine.py", "src/kiro_crew/x.py", "website/y.ts", "docs/guide.md"])


def _selected_ids(base: str) -> list[str]:
    gates = gate.load_map()["gates"]
    return [g["id"] for g in gate.select(gates, gate._changed(base))]


@pytest.mark.parametrize("key", [*HOSTILE_GIT_CONFIG, "core.excludesFile"])
def test_one_hostile_config_key_changes_nothing_from_a_subdirectory(
    branch_repo, tmp_path, monkeypatch, key
) -> None:
    base = _git(branch_repo, "merge-base", "HEAD", "origin/main")
    expected_gates = _selected_ids(base)
    _hostile(branch_repo, tmp_path, only=key)
    for where in (branch_repo, branch_repo / "website", branch_repo / "src" / "kiro_crew"):
        monkeypatch.chdir(where)
        assert gate._changed(base) == EXPECTED_CHANGED, (key, where)
        assert _selected_ids(base) == expected_gates, (key, where)
        assert gate._worktree_state()[2] == [], (key, where)
        assert Path(gate.toplevel()) == branch_repo


def test_every_hostile_key_at_once_changes_nothing_from_a_subdirectory(
    branch_repo, tmp_path, monkeypatch
) -> None:
    base = _git(branch_repo, "merge-base", "HEAD", "origin/main")
    expected_gates = _selected_ids(base)
    assert {"mypy", "flake8", "isort", "agent-sdk-boundary", "sync-io-in-async"} <= set(
        expected_gates
    )
    _hostile(branch_repo, tmp_path)
    monkeypatch.chdir(branch_repo / "website")
    assert gate._changed(base) == EXPECTED_CHANGED
    assert _selected_ids(base) == expected_gates


def test_the_runner_run_from_a_subdirectory_runs_the_same_path_gated_gate(
    branch_repo, tmp_path, use_gates, monkeypatch
) -> None:
    _hostile(branch_repo, tmp_path)
    use_gates(SRC_ONLY)
    for where in (branch_repo, branch_repo / "website"):
        monkeypatch.chdir(where)
        assert gate.main(["pre_push_gate.py"]) == gate.EXIT_FAILED, where


def test_dirty_files_are_found_from_a_subdirectory_under_hostile_config(
    branch_repo, tmp_path, use_gates, monkeypatch, capsys
) -> None:
    use_gates(OK_GATE)
    _hostile(branch_repo, tmp_path)
    monkeypatch.chdir(branch_repo / "website")
    (branch_repo / "website" / "new.ts").write_text("a\n", encoding="utf-8")
    (branch_repo / "src" / "kiro_crew" / "x.py").write_text("y = 2\n", encoding="utf-8")
    assert gate._worktree_state()[2] == [" M src/kiro_crew/x.py", "?? website/new.ts"]
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_UNRUNNABLE
    out = capsys.readouterr().out
    assert "website/new.ts" in out and "src/kiro_crew/x.py" in out
    assert not _proof(branch_repo).exists()


def test_a_clean_run_from_a_subdirectory_under_hostile_config_writes_the_proof(
    branch_repo, tmp_path, use_gates, monkeypatch
) -> None:
    use_gates(OK_GATE)
    _hostile(branch_repo, tmp_path)
    monkeypatch.chdir(branch_repo / "website")
    assert gate.main(["pre_push_gate.py"]) == 0
    assert json.loads(_proof(branch_repo).read_text(encoding="utf-8"))["tree"] == _git(
        branch_repo, "rev-parse", "HEAD^{tree}"
    )
