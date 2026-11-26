"""The ``kirocrew-prepare-pr`` scripts start git once for what one start can answer.

A process start is what a user waits for (about 0.3 s each on Windows).
``pre_push_gate.py`` asks for the work tree's root once a run and hands it down;
the root is an argument of the run and never a value of the module, because one
process may run the script in two repositories and the second run must not read
the first one's. ``local_review.py`` reads a review's base-side files through one
``git cat-file --batch`` process, and each answer is what ``git show`` gives.
"""

from __future__ import annotations

import json
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
SCRIPTS = (
    ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-prepare-pr"
    / "scripts"
)

gate = load_skill_script("prepare_pr_pre_push_gate_git_starts", SCRIPTS / "pre_push_gate.py")
local_review = load_skill_script("prepare_pr_local_review_git_starts", SCRIPTS / "local_review.py")

OK_GATE = {"id": "ok", "cmd": ["{py}", "-c", "pass"], "ci": {"file": "ci.yml", "text": "x"}}
ROOT_ARGS = ["rev-parse", "--show-toplevel"]


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


def _make_repo(parent: Path, name: str, body: str) -> Path:
    """A branch one commit past a synthetic ``origin/main``; ``body`` makes its tree its own."""
    repo = parent / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "sub").mkdir()
    (repo / "sub" / "mine.py").write_text(body, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")
    return repo


@pytest.fixture
def repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Two repositories with different trees, and one passing gate in the map."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    path = tmp_path / "gate_map.json"
    path.write_text(json.dumps({"gates": [OK_GATE], "ci_only": []}), encoding="utf-8")
    monkeypatch.setattr(gate, "MAP_PATH", str(path))
    return _make_repo(tmp_path, "one", "x = 1\n"), _make_repo(tmp_path, "two", "x = 2\n")


@pytest.fixture
def starts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], str | None]]:
    """Every git start the script makes: its arguments and the root it ran from."""
    seen: list[tuple[list[str], str | None]] = []
    real = gate._git_stdout

    def recording(args: list[str], top: str | None) -> bytes:
        seen.append((list(args), top))
        return real(args, top)

    monkeypatch.setattr(gate, "_git_stdout", recording)
    return seen


def _proof(repo: Path) -> dict:
    return json.loads((repo / ".git" / gate.PROOF_FILENAME).read_text(encoding="utf-8"))


def test_a_run_asks_for_the_root_once_and_every_read_starts_there(
    repos: tuple[Path, Path],
    starts: list[tuple[list[str], str | None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one, _two = repos
    monkeypatch.chdir(one / "sub")
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_OK
    root_asks = [top for args, top in starts if args == ROOT_ARGS]
    # Asked once, from the current directory: the one start without ``-C``.
    assert root_asks == [None]
    others = [(args, top) for args, top in starts if args != ROOT_ARGS]
    assert others
    for args, top in others:
        assert top is not None and Path(top).samefile(one), args


def test_verify_asks_for_the_root_once(
    repos: tuple[Path, Path],
    starts: list[tuple[list[str], str | None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one, _two = repos
    monkeypatch.chdir(one)
    assert gate.main(["pre_push_gate.py", "--verify"]) == gate.EXIT_NO_PROOF
    assert [args for args, _top in starts] == [
        ROOT_ARGS,
        ["rev-parse", "--absolute-git-dir"],
        ["rev-parse", "HEAD:"],
    ]


def test_the_commit_and_its_tree_are_one_start_with_the_answers_of_two(
    repos: tuple[Path, Path],
    starts: list[tuple[list[str], str | None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one, _two = repos
    monkeypatch.chdir(one)
    head, tree, dirty = gate._worktree_state()
    assert [args for args, _top in starts if args[0] == "rev-parse" and args != ROOT_ARGS] == [
        ["rev-parse", "HEAD", "HEAD:"]
    ]
    assert (head, tree, dirty) == (
        _git(one, "rev-parse", "HEAD"),
        _git(one, "rev-parse", "HEAD:"),
        [],
    )
    assert head != tree and len(head) == len(tree) == 40
    assert gate.head_tree() == tree


def test_a_second_run_in_another_repository_reads_that_repository(
    repos: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One process, two repositories: nothing of the first run answers for the second."""
    one, two = repos
    monkeypatch.chdir(one)
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_OK
    first = capsys.readouterr().out
    assert _proof(one)["tree"] == _git(one, "rev-parse", "HEAD:")

    monkeypatch.chdir(two)
    # The second repository has no proof yet; the first one's must not be found for it.
    assert gate.main(["pre_push_gate.py", "--verify"]) == gate.EXIT_NO_PROOF
    assert "NO PROOF" in capsys.readouterr().out
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_OK
    second = capsys.readouterr().out
    assert _proof(two)["tree"] == _git(two, "rev-parse", "HEAD:")
    assert _proof(two)["tree"] != _proof(one)["tree"]
    assert first != second

    # Back and forth once more: a file only the second repository has stops only its run.
    (two / "stray.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.chdir(one)
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_OK
    capsys.readouterr()
    monkeypatch.chdir(two)
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_UNRUNNABLE
    assert "stray.txt" in capsys.readouterr().out


def test_helpers_called_without_a_root_ask_git_each_time(
    repos: tuple[Path, Path],
    starts: list[tuple[list[str], str | None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No root is remembered between calls: a helper used on its own asks where it is."""
    one, two = repos
    monkeypatch.chdir(one)
    tree_one = gate.head_tree()
    monkeypatch.chdir(two)
    tree_two = gate.head_tree()
    assert tree_one == _git(one, "rev-parse", "HEAD:")
    assert tree_two == _git(two, "rev-parse", "HEAD:")
    assert [top for args, top in starts if args == ROOT_ARGS] == [None, None]


def test_a_repository_with_no_commit_fails_as_one_git_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``rev-parse HEAD HEAD:`` fails on the first name, as ``rev-parse HEAD`` does."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q", "-b", "main")
    monkeypatch.chdir(empty)
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_ENV
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("ERROR: git rev-parse failed (128): ")
    assert "ambiguous argument 'HEAD'" in captured.err
    assert "HEAD:" not in captured.err


def test_an_answer_that_is_not_a_commit_and_a_tree_is_an_error(
    repos: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One name where two were asked for is never read as a sha with an empty tree."""
    one, _two = repos
    monkeypatch.chdir(one)
    real = gate._git_stdout

    def one_name(args: list[str], top: str | None) -> bytes:
        if args == ["rev-parse", "HEAD", "HEAD:"]:
            return real(["rev-parse", "HEAD"], top)
        return real(args, top)

    monkeypatch.setattr(gate, "_git_stdout", one_name)
    with pytest.raises(ValueError, match="printed 1 names"):
        gate._worktree_state()


# --------------------------------------------------------------------------
# local_review.py: a review's base-side files come from one git process
# --------------------------------------------------------------------------
BLOBS: dict[str, bytes] = {
    "plain.md": b"one\ntwo\n",
    "empty.md": b"",
    "no-final-newline.md": b"one",
    "crlf.md": b"one\r\ntwo\r\n",
    "lone-cr.md": b"one\rtwo\r",
    "not-utf8.md": b"caf\xe9 \xff\xfe\n",
    "bom.md": b"\xef\xbb\xbfone\n",
    "nul.bin": b"a\x00b\n",
    "big.md": b"0123456789abcdef" * 20000,
    "with space.md": b"space\n",
    "dir/inner.md": b"inner\n",
    "café.md": b"name\n",
}
# Names git accepts in a tree and a Windows file system does not; the tree is built in the
# index, so they are only left out where git itself refuses them.
if sys.platform != "win32":
    BLOBS.update(
        {
            'quo"te.md': b"quote\n",
            "it's.md": b"apostrophe\n",
            "line\nbreak.md": b"newline in the name\n",
            "st*r.md": b"glob in the name\n",
            "back\\slash.md": b"backslash in the name\n",
        }
    )
#: What ``git show`` has to answer itself: its exit status and words are git's own.
NOT_FROM_THE_BATCH = {"dir", "gone.md", "module", "line\nbreak.md", "st*r.md", "back\\slash.md"}


@pytest.fixture(scope="module")
def base_repo(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """A repository whose one commit holds ``BLOBS`` and a submodule entry, and its sha."""
    repo = tmp_path_factory.mktemp("base-files") / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for path, body in BLOBS.items():
        done = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=repo,
            check=True,
            capture_output=True,
            input=body,
            env=_git_env(),
        )
        blob = done.stdout.decode("ascii").strip()
        _git(repo, "update-index", "--add", "--cacheinfo", "100644,{},{}".format(blob, path))
    _git(repo, "update-index", "--add", "--cacheinfo", "160000,{},module".format("1" * 40))
    _git(repo, "commit", "-q", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


class _Reads:
    """A ``BaseFiles`` reader over ``repo`` that records every git it starts."""

    def __init__(self, repo: Path) -> None:
        self.shows: list[str] = []
        self.batches = 0
        self.repo = repo
        self.reader = local_review._base_files.BaseFiles(self._run, self._resolve, str(repo))

    def _run(self, args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
        assert args[:2] == ["git", "show"] and cwd == str(self.repo), args
        self.shows.append(args[2].partition(":")[2])
        return local_review.run(args, cwd=cwd)

    def _resolve(self, args: list[str]):
        assert args == ["git", "cat-file", "--batch"], args
        self.batches += 1
        return local_review._tool_argv.resolve(args)


def _git_show(repo: Path, sha: str, path: str) -> tuple[int, str, str]:
    return local_review.run(["git", "show", "{}:{}".format(sha, path)], cwd=str(repo))


def test_every_base_file_reads_as_git_show_reads_it(base_repo: tuple[Path, str]) -> None:
    repo, sha = base_repo
    reads = _Reads(repo)
    names = [*BLOBS, "dir", "gone.md", "module"]
    with reads.reader as reader:
        for name in names:
            assert reader.show(sha, name) == _git_show(repo, sha, name), name
        # A second pass: the process answers in step, whatever was asked before.
        for name in reversed(names):
            assert reader.show(sha, name) == _git_show(repo, sha, name), name
    # Text mode, as ``run`` reads a command: replacement characters, and one kind of line end.
    assert _git_show(repo, sha, "crlf.md") == (0, "one\ntwo\n", "")
    assert _git_show(repo, sha, "lone-cr.md") == (0, "one\ntwo\n", "")
    assert _git_show(repo, sha, "not-utf8.md") == (0, "caf� ��\n", "")
    assert _git_show(repo, sha, "empty.md") == (0, "", "")
    assert _git_show(repo, sha, "gone.md")[0] != 0
    # One process for all of them, and git show only where the batch cannot say what it says.
    assert reads.batches == 1
    assert set(reads.shows) == NOT_FROM_THE_BATCH & set(names)
    assert len(reads.shows) == 2 * len(set(reads.shows))


@pytest.mark.skipif(sys.platform == "win32", reason="a file name cannot hold ':' on Windows")
def test_a_work_tree_file_named_like_the_object_is_left_to_git_show(
    base_repo: tuple[Path, str], tmp_path: Path
) -> None:
    """``git show`` refuses a name that is both a revision and a file; so does the reader."""
    template, sha = base_repo
    repo = Path(shutil.copytree(template, tmp_path / "repo"))
    (repo / "{}:plain.md".format(sha)).write_text("in the work tree\n", encoding="utf-8")
    reads = _Reads(repo)
    with reads.reader as reader:
        answer = reader.show(sha, "plain.md")
        assert reader.show(sha, "empty.md") == (0, "", "")
    assert answer == _git_show(repo, sha, "plain.md")
    assert answer[0] != 0 and "ambiguous" in answer[2]
    assert reads.shows == ["plain.md"]


def test_a_reader_that_is_closed_or_has_no_repository_still_answers_as_git_show(
    base_repo: tuple[Path, str], tmp_path: Path
) -> None:
    repo, sha = base_repo
    reads = _Reads(repo)
    assert reads.reader.show(sha, "plain.md") == (0, "one\ntwo\n", "")
    reads.reader.close()
    reads.reader.close()
    assert reads.reader.show(sha, "plain.md") == (0, "one\ntwo\n", "")
    assert (reads.batches, reads.shows) == (1, ["plain.md"])

    outside = tmp_path / "not-a-repository"
    outside.mkdir()
    nowhere = _Reads(outside)
    with nowhere.reader as reader:
        for _ in range(2):
            assert reader.show("0" * 40, "plain.md") == _git_show(outside, "0" * 40, "plain.md")
    # The batch process ended without an answer: it is not started again.
    assert (nowhere.batches, nowhere.shows) == (1, ["plain.md", "plain.md"])


def test_a_reader_whose_git_must_not_run_hands_back_the_refusal(tmp_path: Path) -> None:
    refusal = (126, "", "git must not run here")
    asked: list[list[str]] = []

    def run(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
        asked.append(args)
        return refusal

    reader = local_review._base_files.BaseFiles(run, lambda args: (args, refusal), str(tmp_path))
    with reader:
        assert reader.show("0" * 40, "plain.md") == refusal
        assert reader.show("0" * 40, "other.md") == refusal
    assert [args[:2] for args in asked] == [["git", "show"], ["git", "show"]]


WORKFLOWS = ROOT / ".github" / "workflows"
PROMPTS = ROOT / ".github" / "review-prompts"


@pytest.fixture(scope="module")
def review_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """This repository's two reviewer workflows and their prompt files on ``main``, and a branch."""
    repo = tmp_path_factory.mktemp("one-review") / "repo"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "review-prompts").mkdir(parents=True)
    for name in ("codex-review.yml", "claude-review.yml"):
        shutil.copy(WORKFLOWS / name, repo / ".github" / "workflows" / name)
    for prompt in sorted(PROMPTS.glob("*.md")):
        shutil.copy(prompt, repo / ".github" / "review-prompts" / prompt.name)
    (repo / "AUTOSDE.yaml").write_text("rules: []\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "changed.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat(thing): add VALUE")
    return repo


def _written(summary: dict, *roots: Path) -> dict[str, str]:
    """Every file a review wrote, by its path below the run's directory."""
    files = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                files[path.relative_to(root.parent).as_posix()] = path.read_text(encoding="utf-8")
    assert files and summary["tasks"]
    return files


def test_one_review_starts_one_batch_and_writes_what_one_git_show_a_file_writes(
    review_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    real_run, real_resolve = local_review.run, local_review._tool_argv.resolve
    shows: list[str] = []
    batches: list[list[str]] = []

    def run(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
        if args[0] == "gh":
            return 127, "", "gh: not found"
        if args[:2] == ["git", "show"] and re.match(r"[0-9a-f]{40}:", args[2]):
            shows.append(args[2].partition(":")[2])
        return real_run(args, cwd=cwd)

    def resolve(args: list[str]):
        if args[:3] == ["git", "cat-file", "--batch"]:
            batches.append(args)
        return real_resolve(args)

    monkeypatch.setattr(local_review, "run", run)
    monkeypatch.setattr(local_review._tool_argv, "resolve", resolve)

    def review(name: str) -> tuple[dict, dict[str, str]]:
        # The same directory names under each run's own parent, so the paths the briefs
        # quote differ only in that parent.
        home = tmp_path / name
        out, stage = home / "run" / "out", home / "run" / "stage"
        summary = local_review.assemble(str(review_repo), "main", str(out), str(stage))
        files = _written(summary, out, stage)
        # Each run draws its own nonce for the intent markers.
        return summary, {
            path: re.sub(r"::[0-9a-f]{32}\b", "::<nonce>", text.replace(str(home), "<home>"))
            for path, text in files.items()
        }

    summary, batched = review("batched")
    assert sorted(summary["tasks"]) == ["gpt", "opus"]
    assert len(batches) == 1
    # Only what is not a file of the base commit went to ``git show``.
    base_files = set(_git(review_repo, "ls-tree", "-r", "--name-only", "main").splitlines())
    assert not set(shows) & base_files

    # The same review with every base file read by its own ``git show``.
    class OneGitAFile:
        def __init__(self, _run, _resolve, worktree: str) -> None:
            self.worktree = worktree

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            return None

        def show(self, base_sha: str, relpath: str) -> tuple[int, str, str]:
            return run(["git", "show", "{}:{}".format(base_sha, relpath)], cwd=self.worktree)

    shows.clear()
    monkeypatch.setattr(local_review._base_files, "BaseFiles", OneGitAFile)
    _summary, one_by_one = review("one-by-one")
    assert len(batches) == 1
    assert set(shows) & base_files
    assert one_by_one == batched


def test_the_batch_process_is_ended_when_the_review_fails(
    review_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    made = []
    real = local_review._base_files.BaseFiles

    class Kept(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args) -> None:
            super().__init__(*args)
            made.append(self)

    monkeypatch.setattr(local_review._base_files, "BaseFiles", Kept)
    seen = []

    def no_shape(workflow_text: str):
        # Reached after the lane's contract was read, so the batch process is running.
        seen.append(made[0]._process)
        return None

    monkeypatch.setattr(local_review, "_prompt_target", no_shape)
    monkeypatch.setattr(local_review, "extract_prompt_file_specs", lambda _text: [])
    with pytest.raises(local_review.ParityError, match="matches neither extraction shape"):
        local_review.assemble(
            str(review_repo), "main", str(tmp_path / "out"), str(tmp_path / "stage")
        )
    assert len(made) == 1 and len(seen) == 1
    process = seen[0]
    assert process is not None and process.returncode is not None
    assert made[0]._process is None
