"""``check_fix_contract.py`` — the scope a security fix was dispatched with.

The gap this closes is one real round. A fix for a cron seam also added a name to
``sandbox._AGENT_DENIED_ENV_KEYS``, which stripped the OPERATOR's own
``KIROCREW_SECURITY_POLICY`` from every agent child. The proof of concept stopped
reproducing. Every ``shell`` row in the golden-path corpus was still permitted,
because none of them is a bash command that can notice an env var going missing. So
``verify_fix.py`` exited 0 on a change that made the product worse, and a human
review lane caught what the gate could not.

Scope is knowable BEFORE the fix is written, which is the whole idea here: the
conductor declares the blast radius in a ``fix-contract.json`` it writes into the
fixer's worktree, and this script asserts the fix against it. The properties pinned
below are the three rejections — a forbidden path, a path outside the allowed set, a
count over the ceiling — and the rule that outranks them: a contract nobody could
read checked nothing, and nothing is never a pass.

The script is driven as a SUBPROCESS against a REAL git checkout, because what it
reports is git's own answer about what changed; a fake ``.git`` marker would pass the
screen and then make every diff unreadable. :func:`build_repo` is shared with
``test_security_conductor_verify_fix.py``, which needs the same checkout to exercise
the contract step through the gate that calls this one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "src" / "kiro_crew" / "builtin_skills" / "security-conductor"
SCRIPT = SKILL_DIR / "scripts" / "check_fix_contract.py"

CONTRACT_FILENAME = "fix-contract.json"

EXIT_HONOURED = 0
EXIT_VIOLATED = 30
EXIT_UNREADABLE = 20
EXIT_INVALID = 2

#: The shape the conductor writes. Every test starts from this and changes one
#: thing, so what a case is about is the difference rather than the whole document.
CONTRACT = {
    "finding_ids": [16],
    "allowed_paths": ["src/kiro_crew/slack/gateway.py", "test/"],
    "forbidden_paths": ["src/kiro_crew/sandbox.py"],
    "max_changed_files": 3,
    "no_new_refusal_statement": "No command a maintainer runs today starts being refused.",
}


@pytest.fixture
def mod():
    return load_skill_script("security_conductor_check_fix_contract", SCRIPT)


def git(worktree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    ).stdout


def write_contract(worktree: Path, contract: dict) -> Path:
    path = worktree / CONTRACT_FILENAME
    path.write_text(json.dumps(contract), encoding="utf-8")
    return path


def build_repo(
    root: Path,
    name: str,
    *,
    base_files: tuple[str, ...] = (),
    committed: tuple[str, ...] = (),
    staged: tuple[str, ...] = (),
    contract: dict | None = None,
    contract_committed: bool = False,
) -> Path:
    """A real git checkout whose ``origin/main`` sits one commit behind HEAD.

    ``base_files`` land in the base commit itself, so they are tracked at HEAD and
    absent from the judged range -- the only way to have a path git knows and this PR
    did not touch. ``committed`` lands in a commit on top of that base and is what
    ``<merge-base>..HEAD`` reports; ``staged`` is written and added to the index but
    never committed, which is how an untracked proof-of-concept copy reaches the
    gate. The remote ref is written by hand because the default base is
    ``origin/main`` and a scratch repository has no remote.

    Shared with the ``verify_fix.py`` suite: both need a checkout git can answer
    about, and a second copy of this would be a second thing to keep correct.
    """
    worktree = root / name
    worktree.mkdir(parents=True)
    git(worktree, "init", "-q", "-b", "work")
    git(worktree, "config", "user.email", "harness@example.invalid")
    git(worktree, "config", "user.name", "harness")
    (worktree / "README.md").write_text("base\n", encoding="utf-8")
    for relative in base_files:
        target = worktree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("already on main\n", encoding="utf-8")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", "base")
    git(
        worktree,
        "update-ref",
        "refs/remotes/origin/main",
        git(worktree, "rev-parse", "HEAD").strip(),
    )
    if contract is not None and contract_committed:
        write_contract(worktree, contract)
    for relative in committed:
        target = worktree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fix\n", encoding="utf-8")
    if committed or (contract is not None and contract_committed):
        git(worktree, "add", "-A")
        git(worktree, "commit", "-q", "-m", "fix")
    for relative in staged:
        target = worktree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("staged\n", encoding="utf-8")
        git(worktree, "add", relative)
    # Written AFTER the commit by default: the conductor drops the contract into the
    # worktree, so it is untracked, and a harness that committed it would make every
    # case exercise the committed-file branch instead.
    if contract is not None and not contract_committed:
        write_contract(worktree, contract)
    return worktree


@pytest.fixture
def repo(tmp_path: Path):
    def build(name: str, **kwargs) -> Path:
        return build_repo(tmp_path, name, **kwargs)

    return build


def run(worktree: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, str(SCRIPT), "--worktree", str(worktree), *extra]
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=120)


def payload(result: subprocess.CompletedProcess[str]) -> dict:
    lines = result.stdout.strip().splitlines()
    assert lines, f"no JSON on stdout; stderr was: {result.stderr}"
    return json.loads(lines[-1])


class TestAFixInsideTheRadiusIsHonoured:
    def test_allowed_paths_pass(self, repo) -> None:
        worktree = repo(
            "inside",
            committed=("src/kiro_crew/slack/gateway.py", "test/test_cron_env.py"),
            contract=CONTRACT,
        )
        result = run(worktree)
        assert result.returncode == EXIT_HONOURED, result.stderr
        body = payload(result)
        assert body["verdict"] == "honoured"
        assert body["changed"] == ["src/kiro_crew/slack/gateway.py", "test/test_cron_env.py"]
        assert body["violations"] == {"forbidden": [], "outside_allowed": [], "count": None}

    def test_the_payload_echoes_the_contract_it_enforced(self, repo) -> None:
        """A reviewer must see WHICH allowed set produced the verdict.

        The contract lives in the worktree, so a fixer could rewrite it, and nothing
        here pretends to be a fence against that. What makes the verdict reviewable
        is that the payload carries the declaration the gate actually applied.
        """
        worktree = repo("echo", committed=("test/test_x.py",), contract=CONTRACT)
        body = payload(run(worktree))
        assert body["contract"]["finding_ids"] == [16]
        assert body["contract"]["max_changed_files"] == 3
        assert body["contract"]["forbidden_paths"] == ["src/kiro_crew/sandbox.py"]
        assert body["contract"]["no_new_refusal_statement"] == (
            CONTRACT["no_new_refusal_statement"]
        )

    def test_a_prefix_matches_whole_segments_only(self, repo) -> None:
        """Forbidding ``sandbox.py`` must not swallow ``sandbox_pod.py``.

        A string ``startswith`` would reject the sibling file, which is a false
        rejection of a fix — the failure mode this whole gate exists to avoid, one
        level up.
        """
        worktree = repo(
            "segment",
            committed=("src/kiro_crew/sandbox_pod.py",),
            contract={**CONTRACT, "allowed_paths": ["src/"]},
        )
        result = run(worktree)
        assert result.returncode == EXIT_HONOURED, result.stderr
        assert payload(result)["violations"]["forbidden"] == []

    def test_the_contract_file_itself_is_not_a_changed_path(self, repo) -> None:
        """The conductor put it there; counting it would spend one of the fixer's files."""
        worktree = repo(
            "own-file",
            committed=("test/test_x.py",),
            contract=CONTRACT,
            contract_committed=True,
        )
        body = payload(run(worktree))
        assert CONTRACT_FILENAME in body["ignored"]
        assert CONTRACT_FILENAME not in body["changed"]

    def test_a_staged_poc_copy_is_ignored(self, repo) -> None:
        """``verify_fix.py``'s evaluator needs the audit round's PoC file present.

        That copy is scaffolding for the gate rather than part of the fix, and it
        reaches the checkout staged or untracked rather than committed.
        """
        worktree = repo(
            "poc",
            committed=("src/kiro_crew/slack/gateway.py",),
            staged=("test/test_s10_cron_env_governance_escape.py",),
            contract={**CONTRACT, "allowed_paths": ["src/kiro_crew/slack/gateway.py"]},
        )
        result = run(worktree)
        assert result.returncode == EXIT_HONOURED, result.stderr
        body = payload(result)
        assert "test/test_s10_cron_env_governance_escape.py" in body["ignored"]
        assert body["changed"] == ["src/kiro_crew/slack/gateway.py"]

    def test_a_tracked_poc_shaped_file_is_judged_even_when_only_edited(self, repo) -> None:
        """The exemption is for an UNTRACKED scaffolding copy, not for the name's shape.

        A PoC-shaped path the repository already tracks is part of the project's own
        test suite, so an edit to it is an edit a contract has to allow -- and exempting
        it because this range did not commit it would let an out-of-scope change ride
        along under a filename.
        """
        worktree = repo(
            "tracked-poc",
            base_files=("test/test_s10_cron_env_governance_escape.py",),
            committed=("src/kiro_crew/slack/gateway.py",),
            contract={**CONTRACT, "allowed_paths": ["src/kiro_crew/slack/gateway.py"]},
        )
        (worktree / "test" / "test_s10_cron_env_governance_escape.py").write_text(
            "edited out of scope\n", encoding="utf-8"
        )
        result = run(worktree)
        assert result.returncode == EXIT_VIOLATED, result.stdout
        body = payload(result)
        assert body["violations"]["outside_allowed"] == [
            "test/test_s10_cron_env_governance_escape.py"
        ]
        assert "test/test_s10_cron_env_governance_escape.py" not in body["ignored"]

    def test_a_committed_poc_shaped_file_is_judged(self, repo) -> None:
        """Once it is in the range it is the fix's own test, and the contract must say so."""
        worktree = repo(
            "committed-poc",
            committed=("test/test_s10_cron_env_governance_escape.py",),
            contract={**CONTRACT, "allowed_paths": ["src/kiro_crew/slack/gateway.py"]},
        )
        result = run(worktree)
        assert result.returncode == EXIT_VIOLATED
        assert payload(result)["violations"]["outside_allowed"] == [
            "test/test_s10_cron_env_governance_escape.py"
        ]


class TestARenameShowsBothOfItsEnds:
    """Rename detection reports only the destination, which hid the source path.

    Moving a forbidden file under an allowed prefix would otherwise be judged by its new
    name alone, and the contract would report honoured on a change that touched the one
    file it was told to leave alone.
    """

    def test_a_forbidden_file_renamed_under_an_allowed_prefix_is_still_rejected(self, repo) -> None:
        worktree = repo(
            "renamed",
            base_files=("src/kiro_crew/sandbox.py",),
            contract={**CONTRACT, "allowed_paths": ["src/kiro_crew/allowed/"]},
        )
        (worktree / "src" / "kiro_crew" / "allowed").mkdir(parents=True)
        git(worktree, "mv", "src/kiro_crew/sandbox.py", "src/kiro_crew/allowed/sandbox.py")
        git(worktree, "commit", "-q", "-m", "move the forbidden file somewhere allowed")
        result = run(worktree)
        assert result.returncode == EXIT_VIOLATED, result.stdout
        body = payload(result)
        assert body["violations"]["forbidden"] == ["src/kiro_crew/sandbox.py"]
        assert "src/kiro_crew/allowed/sandbox.py" in body["changed"]

    def test_both_diffs_disable_rename_detection(self) -> None:
        """Pinned in the source, because the failure is silent when the flag is dropped."""
        assert SCRIPT.read_text(encoding="utf-8").count('"--no-renames"') == 2


class TestLeavingTheRadiusIsARejection:
    def test_a_forbidden_path_is_thirty(self, repo) -> None:
        """THE ROUND: the fix reached into the file it was told to leave alone."""
        worktree = repo(
            "forbidden",
            committed=("src/kiro_crew/slack/gateway.py", "src/kiro_crew/sandbox.py"),
            contract=CONTRACT,
        )
        result = run(worktree)
        assert result.returncode == EXIT_VIOLATED
        body = payload(result)
        assert body["verdict"] == "violated"
        assert body["violations"]["forbidden"] == ["src/kiro_crew/sandbox.py"]
        assert "src/kiro_crew/sandbox.py" in result.stderr

    def test_a_path_outside_the_allowed_set_is_thirty(self, repo) -> None:
        worktree = repo("outside", committed=("src/kiro_crew/security.py",), contract=CONTRACT)
        result = run(worktree)
        assert result.returncode == EXIT_VIOLATED
        assert payload(result)["violations"]["outside_allowed"] == ["src/kiro_crew/security.py"]

    def test_too_many_files_is_thirty_even_when_each_is_allowed(self, repo) -> None:
        """A fix that stayed inside ``test/`` but rewrote nine files is not minimal."""
        worktree = repo(
            "over-count",
            committed=("test/a.py", "test/b.py", "test/c.py", "test/d.py"),
            contract={**CONTRACT, "max_changed_files": 3},
        )
        result = run(worktree)
        assert result.returncode == EXIT_VIOLATED
        body = payload(result)
        assert body["violations"]["count"] == {"changed": 4, "max": 3}
        assert body["violations"]["outside_allowed"] == []

    def test_a_staged_but_uncommitted_change_still_counts(self, repo) -> None:
        """A fix staged and not yet committed is still the fix."""
        worktree = repo(
            "staged-violation",
            committed=("test/test_x.py",),
            staged=("src/kiro_crew/sandbox.py",),
            contract=CONTRACT,
        )
        result = run(worktree)
        assert result.returncode == EXIT_VIOLATED
        assert payload(result)["violations"]["forbidden"] == ["src/kiro_crew/sandbox.py"]

    def test_every_violation_is_reported_in_one_run(self, repo) -> None:
        """A partial report costs a round, and every fixer round is a human's yes."""
        worktree = repo(
            "all-three",
            committed=(
                "src/kiro_crew/sandbox.py",
                "src/kiro_crew/security.py",
                "docs/notes.md",
                "README.md",
            ),
            contract={**CONTRACT, "max_changed_files": 2},
        )
        body = payload(run(worktree))
        assert body["violations"]["forbidden"] == ["src/kiro_crew/sandbox.py"]
        assert len(body["violations"]["outside_allowed"]) == 4
        assert body["violations"]["count"] == {"changed": 4, "max": 2}


class TestAContractNobodyCanReadIsNeverAPass:
    def test_an_absent_contract_is_twenty(self, repo) -> None:
        worktree = repo("absent", committed=("test/test_x.py",), contract=None)
        result = run(worktree)
        assert result.returncode == EXIT_UNREADABLE
        body = payload(result)
        assert body["verdict"] == "unreadable"
        assert "not readable" in body["problems"][0]

    def test_json_that_does_not_parse_is_twenty_and_not_a_traceback(self, repo) -> None:
        worktree = repo("unparsable", committed=("test/test_x.py",))
        (worktree / CONTRACT_FILENAME).write_text("{not json", encoding="utf-8")
        result = run(worktree)
        assert result.returncode == EXIT_UNREADABLE
        assert "Traceback" not in result.stderr
        assert "does not parse" in payload(result)["problems"][0]

    @pytest.mark.parametrize(
        "field, value",
        [
            ("finding_ids", "16"),
            ("finding_ids", []),
            # ``bool`` subclasses ``int``, so ``[true]`` would otherwise be accepted
            # and reported as finding 1.
            ("finding_ids", [True]),
            ("allowed_paths", []),
            ("allowed_paths", ["  "]),
            ("max_changed_files", 0),
            ("max_changed_files", True),
            ("no_new_refusal_statement", 7),
        ],
    )
    def test_a_malformed_field_is_twenty_and_names_itself(self, repo, field, value) -> None:
        worktree = repo("malformed", committed=("test/test_x.py",))
        write_contract(worktree, {**CONTRACT, field: value})
        result = run(worktree)
        assert result.returncode == EXIT_UNREADABLE
        assert any(field in problem for problem in payload(result)["problems"])

    def test_a_prefix_that_is_allowed_and_forbidden_states_no_scope(self, repo) -> None:
        """Resolving it by precedence would be this script choosing the scope itself."""
        worktree = repo("contradiction", committed=("test/test_x.py",))
        write_contract(
            worktree, {**CONTRACT, "allowed_paths": ["test/"], "forbidden_paths": ["test/"]}
        )
        result = run(worktree)
        assert result.returncode == EXIT_UNREADABLE
        assert any("both allowed and forbidden" in p for p in payload(result)["problems"])

    def test_a_base_git_cannot_resolve_is_twenty(self, repo) -> None:
        worktree = repo("nobase", committed=("test/test_x.py",), contract=CONTRACT)
        result = run(worktree, "--base", "origin/does-not-exist")
        assert result.returncode == EXIT_UNREADABLE
        assert "merge-base" in payload(result)["problems"][0]


class TestTheBaseIsTheCallersAlone:
    """The contract has no ``base`` key, deliberately.

    It would be read out of the tree under review, and naming ``HEAD`` there empties
    the judged diff -- so every scope check would be trivially honoured by one line in
    the file the fixer can edit.
    """

    def test_a_base_key_in_the_contract_changes_nothing(self, repo) -> None:
        worktree = repo("stray-base", committed=("src/kiro_crew/security.py",))
        write_contract(worktree, {**CONTRACT, "base": "HEAD"})
        result = run(worktree)
        assert result.returncode == EXIT_VIOLATED, result.stdout
        body = payload(result)
        assert body["base"] == "origin/main"
        assert body["violations"]["outside_allowed"] == ["src/kiro_crew/security.py"]
        assert "base" not in body["contract"]

    def test_an_explicit_base_is_used(self, repo) -> None:
        worktree = repo("explicit-base", committed=("test/test_x.py",))
        write_contract(worktree, CONTRACT)
        result = run(worktree, "--base", "refs/remotes/origin/main")
        assert result.returncode == EXIT_HONOURED, result.stderr
        assert payload(result)["base"] == "refs/remotes/origin/main"

    def test_the_default_is_origin_main(self, mod) -> None:
        assert mod.DEFAULT_BASE == "origin/main"


class TestInvalidInputIsTwoNotAVerdict:
    def test_a_directory_that_is_not_a_checkout_is_two(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        result = run(plain)
        assert result.returncode == EXIT_INVALID
        assert "not a git checkout" in result.stderr

    def test_a_missing_directory_is_two(self, tmp_path: Path) -> None:
        result = run(tmp_path / "nope")
        assert result.returncode == EXIT_INVALID
        assert "not a directory" in result.stderr

    def test_a_named_contract_elsewhere_is_read(self, repo, tmp_path: Path) -> None:
        """The conductor may hold the contract outside a worktree it must not write to."""
        worktree = repo("named", committed=("test/test_x.py",))
        elsewhere = tmp_path / "held" / CONTRACT_FILENAME
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_text(json.dumps(CONTRACT), encoding="utf-8")
        result = run(worktree, "--contract", str(elsewhere))
        assert result.returncode == EXIT_HONOURED, result.stderr
        assert payload(result)["contract_path"] == str(elsewhere)


class TestThePathComparisonHasNoStringEdges:
    """Unit-level, on the two pure helpers the verdict is built from."""

    def test_a_prefix_never_matches_a_partial_segment(self, mod) -> None:
        assert mod.under_prefix("test/test_x.py", "test") is True
        assert mod.under_prefix("test", "test") is True
        assert mod.under_prefix("testdata/secret", "test") is False
        assert (
            mod.under_prefix("src/kiro_crew/sandbox.py.orig", "src/kiro_crew/sandbox.py") is False
        )

    def test_an_empty_prefix_matches_nothing(self, mod) -> None:
        """An empty allowed entry would otherwise permit the whole tree."""
        assert mod.under_prefix("anything", "") is False

    def test_a_windows_spelling_normalises_to_the_paths_git_reports(self, mod) -> None:
        assert mod.normalise_path("src\\kiro_crew\\sandbox.py") == "src/kiro_crew/sandbox.py"
        assert mod.normalise_path("./test/") == "test"


class TestAnUnstagedEditIsStillTheFix:
    """The proof and the behaviour rows execute the LIVE worktree.

    So an out-of-scope edit left unstaged is code the verification RAN against and
    the commit does not carry. Judging only the commit and the index would report
    ``honoured`` for a fix whose verified behaviour is not the fix under review.
    """

    def test_an_unstaged_tracked_edit_outside_the_radius_is_thirty(self, repo) -> None:
        worktree = repo("unstaged", committed=("test/test_x.py",), contract=CONTRACT)
        # A tracked file from the base commit, edited and left unstaged.
        (worktree / "README.md").write_text("edited but never staged\n", encoding="utf-8")
        result = run(worktree)
        assert result.returncode == EXIT_VIOLATED, result.stdout
        assert payload(result)["violations"]["outside_allowed"] == ["README.md"]

    def test_an_unstaged_edit_inside_the_radius_still_passes(self, repo) -> None:
        """Including the unstaged half must not refuse a fix that is in scope."""
        worktree = repo("unstaged-ok", committed=("test/test_x.py",), contract=CONTRACT)
        (worktree / "test" / "test_x.py").write_text("more of the fix\n", encoding="utf-8")
        result = run(worktree)
        assert result.returncode == EXIT_HONOURED, result.stderr
        assert payload(result)["changed"] == ["test/test_x.py"]

    def test_only_the_named_test_run_residue_is_exempt(self, repo) -> None:
        """A Kiro Crew test run drops a data home and a breadcrumb into the tree.

        Refusing those would reject every fixer that ran the suite, which is the false
        rejection this gate exists to avoid one level up -- so they are exempt BY NAME,
        and the skip is reported rather than silent.
        """
        worktree = repo("residue", committed=("test/test_x.py",), contract=CONTRACT)
        (worktree / ".kirocrew.breadcrumb").write_text("test-run residue\n", encoding="utf-8")
        (worktree / ".kiro" / "crew").mkdir(parents=True)
        (worktree / ".kiro" / "crew" / "state.json").write_text("{}\n", encoding="utf-8")
        result = run(worktree)
        assert result.returncode == EXIT_HONOURED, result.stderr
        body = payload(result)
        assert body["changed"] == ["test/test_x.py"]
        assert ".kirocrew.breadcrumb" in body["ignored"]
        assert ".kiro/crew/state.json" in body["ignored"]

    @pytest.mark.parametrize(
        "relative",
        [
            # A module the fix imports, and a data asset it loads: the merged checkout
            # carries neither, and the extension makes no difference to the crash.
            "src/kiro_crew/new_helper.py",
            "src/kiro_crew/lookup_table.json",
        ],
    )
    def test_an_untracked_dependency_is_judged_whatever_its_extension(self, repo, relative) -> None:
        """The crash this closes: verification runs the live worktree, the merge does not."""
        worktree = repo("untracked-dep", committed=("test/test_x.py",), contract=CONTRACT)
        target = worktree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("payload\n", encoding="utf-8")
        result = run(worktree)
        assert result.returncode == EXIT_VIOLATED, result.stdout
        assert payload(result)["violations"]["outside_allowed"] == [relative]

    def test_a_gitignored_file_is_not_judged(self, repo) -> None:
        """The ignore decision is git's own, and this gate takes it at its word.

        A residue it accepts: a fix depending on a gitignored module still breaks the
        merge. The alternative refuses every worktree carrying a ``.venv``,
        ``node_modules`` or a build directory, so the repository's own declaration of what
        is not part of the project wins.
        """
        worktree = repo("ignored-module", committed=("test/test_x.py",), contract=CONTRACT)
        (worktree / ".gitignore").write_text("scratch/\n", encoding="utf-8")
        git(worktree, "add", ".gitignore")
        git(worktree, "commit", "-q", "-m", "ignore scratch")
        (worktree / "scratch").mkdir()
        (worktree / "scratch" / "probe.py").write_text("VALUE = 1\n", encoding="utf-8")
        result = run(worktree)
        assert result.returncode == EXIT_VIOLATED, result.stdout
        # The .gitignore commit itself is the only out-of-scope path; the ignored module
        # is absent from both lists.
        body = payload(result)
        assert body["violations"]["outside_allowed"] == [".gitignore"]
        assert "scratch/probe.py" not in body["changed"]
        assert "scratch/probe.py" not in body["ignored"]
