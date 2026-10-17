"""``verify_fix.py``'s fix-contract step and its ``test`` golden paths.

A fix is judged against the blast radius its conductor declared in ``fix-contract.json``:
the copy the conductor holds outside the worktree and names with ``--contract`` is the one
enforced, a forbidden path is a rejection, an absent sibling, an unreadable contract or one
written for another finding settles nothing, a worktree copy nobody named is unverifiable,
and a reproducing proof still outranks a violated contract. A ``test`` row names a pytest
selector the gate runs against the fixed worktree unless a declared contract failed to
settle as honoured, which a ``conftest.py`` sentinel makes observable. A failing or missing
node is a broken golden path, a node that collects nothing is unverifiable, and a selector
that is absolute, climbs out of the tree, begins with a dash, names a response file or
carries a NUL byte is refused rather than run.

The contract classes stand up a real git checkout with
``test_security_conductor_fix_contract.build_repo``. The staging fixtures, the stub
verifier and fence, :func:`run_fix` and the exit-code constants come from
``security_conductor_verify_fix_helpers.py``; the proof, the deny-fence tiers, the corpus
and the platform filter are pinned in ``test_security_conductor_verify_fix.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from security_conductor_verify_fix_helpers import (  # noqa: F401 -- fixtures are requested by name
    EXIT_BROKEN,
    EXIT_HOLDS,
    EXIT_REPRODUCES,
    EXIT_UNVERIFIABLE,
    VERIFIER_CONFIRMED,
    VERIFIER_REJECTED,
    VERIFY_FIX,
    a_golden_path,
    fence,
    install_verifier,
    mod_fixture,
    payload,
    run_fix,
    staged_fixture,
    worktree_fixture,
)
from test_security_conductor_fix_contract import build_repo

#: A behaviour row's target: a test module in the worktree under review. Written at
#: the call site rather than fixtured, because which of the three bodies a case wants
#: IS the case.
PASSING_TEST = "def test_the_behaviour_holds():\n    assert True\n"
FAILING_TEST = (
    "def test_the_behaviour_holds():\n"
    "    assert False, 'the operator lost their own policy path'\n"
)
NO_TEST_IN_IT = "# a module the fix left behind with no test node in it\n"

#: The contract a conductor writes for a fix like finding 16's: the seam it was sent
#: to, and the file whose edit was the regression. ``finding_ids`` names the finding
#: :func:`run_fix` verifies, because a contract that describes another dispatch is
#: deliberately ``unverifiable`` -- pinned in ``TestTheContractCannotSteerTheGate...``.
A_CONTRACT = {
    "finding_ids": [1],
    "allowed_paths": ["src/", "test/"],
    "forbidden_paths": ["src/kiro_crew/sandbox.py"],
    "max_changed_files": 3,
    "no_new_refusal_statement": "An operator-set KIROCREW_SECURITY_POLICY still reaches the child.",
}


def held_contract(root: Path, name: str, contract: dict) -> Path:
    """The conductor's own copy, written OUTSIDE any worktree.

    Named per case so two cases sharing a ``tmp_path`` cannot share a file, and kept out
    of the worktree because that is the whole property: the enforced declaration is not
    one the fixer can reach.
    """
    held = root / f"{name}-held-contract.json"
    held.write_text(json.dumps(contract), encoding="utf-8")
    return held


def a_behaviour_file(worktree: Path, relative: str, body: str) -> None:
    path = worktree / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


#: The file the conftest below writes, directly under the worktree.
SENTINEL_NAME = "the-conftest-ran.txt"

#: A ``conftest.py`` for the fixer's own worktree that records having been imported.
#: pytest imports the conftest above a selector before collecting anything, so this
#: file existing afterwards means the gate ran pytest out of that worktree, and it
#: being absent means the gate did not. That is the capability a ``test`` row carries,
#: written so a test can assert on it. The path is derived from ``__file__`` rather
#: than an environment variable because ``child_env`` inherits no name a caller sets.
CONFTEST_THAT_RECORDS_ITS_IMPORT = f"""from pathlib import Path

Path(__file__).resolve().parent.parent.joinpath({SENTINEL_NAME!r}).write_text(
    "the conftest ran", encoding="utf-8"
)
"""


class TestTheFixContractIsStepZero:
    """Did the fix stay inside the radius the conductor declared for it?

    A REAL git checkout here, not the bare ``.git`` marker the other classes use:
    what the contract step reports is git's own answer about what changed.
    """

    def test_no_contract_file_behaves_exactly_as_before(self, staged: Path, tmp_path: Path) -> None:
        """An old dispatch must not turn unverifiable overnight."""
        worktree = build_repo(tmp_path, "no-contract", committed=("src/kiro_crew/x.py",))
        a_behaviour_file(worktree, "test/test_behaviour.py", PASSING_TEST)
        a_golden_path(staged, kind="test", command="test/test_behaviour.py")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, tmp_path / "findings.db", worktree)
        assert result.returncode == EXIT_HOLDS, result.stderr
        assert payload(result)["contract"] == {
            "declared": False,
            "verdict": None,
            "why": "no fix contract was declared",
        }

    def test_a_named_contract_that_is_honoured_holds(self, staged: Path, tmp_path: Path) -> None:
        worktree = build_repo(tmp_path, "honoured", committed=("src/kiro_crew/x.py",))
        held = held_contract(tmp_path, "honoured", A_CONTRACT)
        a_behaviour_file(worktree, "test/test_behaviour.py", PASSING_TEST)
        a_golden_path(staged, kind="test", command="test/test_behaviour.py")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(
            staged, tmp_path / "findings.db", worktree, extra=["--contract", str(held)]
        )
        assert result.returncode == EXIT_HOLDS, result.stderr
        body = payload(result)
        assert body["contract"]["declared"] is True
        assert body["contract"]["verdict"] == "holds"
        assert body["contract"]["report"]["verdict"] == "honoured"

    def test_a_forbidden_path_makes_the_fix_broken(self, staged: Path, tmp_path: Path) -> None:
        """THE ROUND: the proof is dead, no golden path is refused, and this is a 30."""
        worktree = build_repo(
            tmp_path,
            "over-reach",
            committed=("src/kiro_crew/x.py", "src/kiro_crew/sandbox.py"),
        )
        held = held_contract(tmp_path, "over-reach", A_CONTRACT)
        a_golden_path(staged, kind="flow", command="monitor_start")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(
            staged, tmp_path / "findings.db", worktree, extra=["--contract", str(held)]
        )
        assert result.returncode == EXIT_BROKEN, result.stdout
        body = payload(result)
        assert body["verdict"] == "broken"
        assert body["contract"]["verdict"] == "broken"
        assert "src/kiro_crew/sandbox.py" in body["contract"]["why"]
        assert "fix contract" in result.stderr

    def test_a_contract_that_will_not_read_is_unverifiable(
        self, staged: Path, tmp_path: Path
    ) -> None:
        """A named contract nobody could read is a check that went unsettled."""
        worktree = build_repo(tmp_path, "bad-contract", committed=("src/kiro_crew/x.py",))
        held = tmp_path / "bad-contract-held.json"
        held.write_text("{not json", encoding="utf-8")
        a_golden_path(staged, kind="flow", command="monitor_start")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(
            staged, tmp_path / "findings.db", worktree, extra=["--contract", str(held)]
        )
        assert result.returncode == EXIT_UNVERIFIABLE, result.stdout
        assert payload(result)["contract"]["verdict"] == "unverifiable"
        assert "Traceback" not in result.stderr

    def test_an_absent_sibling_is_a_broken_installation_not_a_pass(
        self, staged: Path, tmp_path: Path
    ) -> None:
        worktree = build_repo(tmp_path, "no-sibling", committed=("src/kiro_crew/x.py",))
        held = held_contract(tmp_path, "no-sibling", A_CONTRACT)
        (staged / "check_fix_contract.py").unlink()
        a_golden_path(staged, kind="flow", command="monitor_start")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(
            staged, tmp_path / "findings.db", worktree, extra=["--contract", str(held)]
        )
        assert result.returncode == EXIT_UNVERIFIABLE, result.stdout
        assert "broken installation" in payload(result)["contract"]["why"]

    def test_a_reproducing_proof_still_outranks_a_violated_contract(
        self, staged: Path, tmp_path: Path
    ) -> None:
        """Precedence 10 > 30 is unchanged: a fix that did not land is not a scope story."""
        worktree = build_repo(tmp_path, "both-wrong", committed=("src/kiro_crew/sandbox.py",))
        held = held_contract(tmp_path, "both-wrong", A_CONTRACT)
        a_golden_path(staged, kind="flow", command="monitor_start")
        install_verifier(staged, VERIFIER_CONFIRMED)
        result = run_fix(
            staged, tmp_path / "findings.db", worktree, extra=["--contract", str(held)]
        )
        assert result.returncode == EXIT_REPRODUCES, result.stdout
        body = payload(result)
        assert body["verdict"] == "reproduces"
        # Still REPORTED, so the fixer gets one round of feedback rather than two.
        assert body["contract"]["verdict"] == "broken"

    def test_the_filename_is_the_switch_when_no_copy_is_named(self, mod) -> None:
        """With no ``--contract``, the worktree's own file decides whether the step runs."""
        assert mod.CONTRACT_FILENAME == "fix-contract.json"


class TestAnUnsettledContractStopsTheGateRunningTheWorktree:
    """A ``test`` row runs code out of the fixed worktree; the contract licenses it.

    Running a row means pytest imports the named module and the ``conftest.py`` above
    it, with the operator's access. What makes that sound is the contract step: every
    changed path in the worktree sits inside a blast radius a conductor declared
    outside it. A violated or unreadable contract withdraws that, so the rows are
    reported instead of run.

    The sentinel is the assertion. A ``conftest.py`` that writes a file when imported
    turns "pytest was never invoked" into something observable, rather than a claim
    about which branch was taken.
    """

    def a_row_and_its_sentinel(self, staged: Path, worktree: Path) -> Path:
        """One passing ``test`` row, and the conftest that records running it."""
        a_behaviour_file(worktree, "test/test_behaviour.py", PASSING_TEST)
        a_behaviour_file(worktree, "test/conftest.py", CONFTEST_THAT_RECORDS_ITS_IMPORT)
        a_golden_path(staged, kind="test", command="test/test_behaviour.py")
        install_verifier(staged, VERIFIER_REJECTED)
        return worktree / SENTINEL_NAME

    def test_a_violated_contract_runs_no_pytest_at_all(self, staged: Path, tmp_path: Path) -> None:
        """THE ROUND: the fix left its radius, so its worktree is read and not run."""
        worktree = build_repo(
            tmp_path,
            "violated-no-run",
            committed=("src/kiro_crew/x.py", "src/kiro_crew/sandbox.py"),
        )
        sentinel = self.a_row_and_its_sentinel(staged, worktree)
        held = held_contract(tmp_path, "violated-no-run", A_CONTRACT)
        result = run_fix(
            staged, tmp_path / "findings.db", worktree, extra=["--contract", str(held)]
        )
        assert result.returncode == EXIT_BROKEN, result.stdout
        # The proof: the worktree's conftest was never imported, so no pytest ran.
        assert not sentinel.exists(), sentinel.read_text(encoding="utf-8")
        body = payload(result)
        assert body["contract"]["verdict"] == "broken"
        # Reported, not silently dropped -- and not counted as a row that was checked.
        assert body["golden_paths_checked"] == 0
        assert [row["kind"] for row in body["unverifiable"]] == ["test"]
        assert "declared fix contract did not settle as honoured" in (
            body["unverifiable"][0]["why"]
        )

    def test_an_unreadable_contract_runs_no_pytest_either(
        self, staged: Path, tmp_path: Path
    ) -> None:
        """Unreadable is not honoured: the same licence is missing, so the same skip."""
        worktree = build_repo(tmp_path, "unreadable-no-run", committed=("src/kiro_crew/x.py",))
        sentinel = self.a_row_and_its_sentinel(staged, worktree)
        held = tmp_path / "unreadable-held.json"
        held.write_text("{not json", encoding="utf-8")
        result = run_fix(
            staged, tmp_path / "findings.db", worktree, extra=["--contract", str(held)]
        )
        assert result.returncode == EXIT_UNVERIFIABLE, result.stdout
        assert not sentinel.exists(), sentinel.read_text(encoding="utf-8")
        body = payload(result)
        assert body["contract"]["verdict"] == "unverifiable"
        assert body["golden_paths_checked"] == 0
        assert [row["kind"] for row in body["unverifiable"]] == ["test"]

    def test_an_honoured_contract_does_run_the_row(self, staged: Path, tmp_path: Path) -> None:
        """The positive control: the licence is what the skip turns on, not the row.

        Without this, a screen that refused every ``test`` row unconditionally would
        pass the two cases above and cost the corpus its whole behaviour half.
        """
        worktree = build_repo(tmp_path, "honoured-does-run", committed=("src/kiro_crew/x.py",))
        sentinel = self.a_row_and_its_sentinel(staged, worktree)
        held = held_contract(tmp_path, "honoured-does-run", A_CONTRACT)
        result = run_fix(
            staged, tmp_path / "findings.db", worktree, extra=["--contract", str(held)]
        )
        assert result.returncode == EXIT_HOLDS, result.stderr
        assert sentinel.exists(), result.stderr
        body = payload(result)
        assert body["contract"]["verdict"] == "holds"
        assert body["golden_paths_checked"] == 1
        assert body["unverifiable"] == []

    def test_a_shell_row_is_still_classified_under_a_violated_contract(
        self, staged: Path, tmp_path: Path
    ) -> None:
        """Classification reads the fence; it does not run a corpus row through it."""
        worktree = build_repo(
            tmp_path,
            "violated-shell",
            committed=("src/kiro_crew/x.py", "src/kiro_crew/sandbox.py"),
        )
        a_golden_path(staged, kind="shell", command="git status --porcelain")
        install_verifier(staged, VERIFIER_REJECTED)
        held = held_contract(tmp_path, "violated-shell", A_CONTRACT)
        result = run_fix(
            staged,
            tmp_path / "findings.db",
            worktree,
            fence=fence("REFUSE-ME"),
            extra=["--contract", str(held)],
        )
        assert result.returncode == EXIT_BROKEN, result.stdout
        body = payload(result)
        assert body["contract"]["verdict"] == "broken"
        assert body["golden_paths_checked"] == 1
        assert body["unverifiable"] == []


class TestTheTestKindIsRunAgainstTheFix:
    """A behaviour golden path: what a fence classification cannot answer.

    "The operator's own env var still reaches the child" is not a bash command, so a
    corpus of commands had no way to state it -- which is how an over-strict fix
    passed this gate.
    """

    def test_a_passing_behaviour_row_holds_and_is_counted(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        a_behaviour_file(worktree, "test/test_behaviour.py", PASSING_TEST)
        a_golden_path(staged, kind="test", command="test/test_behaviour.py")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, tmp_path / "findings.db", worktree)
        assert result.returncode == EXIT_HOLDS, result.stderr
        body = payload(result)
        assert body["golden_paths_checked"] == 1
        assert body["broken"] == []
        assert body["unverifiable"] == []
        # A test row is CHECKED, so it must not also be handed to a human.
        assert body["needs_human"] == []

    def test_a_node_selector_is_accepted(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """``file::node`` is the shape a behaviour row usually needs."""
        a_behaviour_file(worktree, "test/test_behaviour.py", PASSING_TEST)
        a_golden_path(
            staged, kind="test", command="test/test_behaviour.py::test_the_behaviour_holds"
        )
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, tmp_path / "findings.db", worktree)
        assert result.returncode == EXIT_HOLDS, result.stderr
        assert payload(result)["golden_paths_checked"] == 1

    def test_a_failing_behaviour_row_is_thirty_and_names_the_row(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """The regression this kind exists for: the fix killed a legitimate behaviour."""
        a_behaviour_file(worktree, "test/test_behaviour.py", FAILING_TEST)
        entry = a_golden_path(staged, kind="test", command="test/test_behaviour.py")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, tmp_path / "findings.db", worktree)
        assert result.returncode == EXIT_BROKEN, result.stdout
        body = payload(result)
        assert [row["entry"] for row in body["broken"]] == [entry]
        assert "fails against the fix" in body["broken"][0]["why"]
        assert "broken golden path" in result.stderr

    def test_a_row_that_collects_nothing_is_unverifiable_never_a_pass(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """Zero tests collected measured nothing, which is the vacuous green to avoid."""
        a_behaviour_file(worktree, "test/test_behaviour.py", NO_TEST_IN_IT)
        a_golden_path(staged, kind="test", command="test/test_behaviour.py")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, tmp_path / "findings.db", worktree)
        assert result.returncode == EXIT_UNVERIFIABLE, result.stdout
        body = payload(result)
        assert body["golden_paths_checked"] == 0
        assert "collected no test" in body["unverifiable"][0]["why"]

    def test_a_selector_the_tree_does_not_have_is_thirty(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """A behaviour row naming a file the fix deleted is an actionable rejection."""
        a_golden_path(staged, kind="test", command="test/test_gone.py")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, tmp_path / "findings.db", worktree)
        assert result.returncode == EXIT_BROKEN, result.stdout
        assert "no such test file" in payload(result)["broken"][0]["why"]

    @pytest.mark.parametrize(
        "selector, expected",
        [
            ("/etc/test_elsewhere.py", "relative to the worktree"),
            ("../elsewhere/test_x.py", "climb out"),
            # ``-p evil`` would load a plugin instead of running a test.
            ("-p evil_plugin", "begin with a dash"),
        ],
    )
    def test_a_selector_that_is_not_a_node_of_this_tree_is_refused_not_run(
        self, staged: Path, tmp_path: Path, worktree: Path, selector: str, expected: str
    ) -> None:
        a_golden_path(staged, kind="test", command=selector)
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, tmp_path / "findings.db", worktree)
        assert result.returncode == EXIT_UNVERIFIABLE, result.stdout
        assert expected in payload(result)["unverifiable"][0]["why"]

    def test_the_selector_is_passed_as_a_positional_argument(self, mod) -> None:
        """After ``--``, so a row can never be read as an option by pytest itself."""
        source = VERIFY_FIX.read_text(encoding="utf-8")
        assert '*PYTEST_ARGS, "--", selector' in source
        assert "-n" in mod.PYTEST_ARGS and "0" in mod.PYTEST_ARGS

    def test_no_flag_can_skip_a_behaviour_row(self, mod) -> None:
        """One would let the fixer choose which half of the corpus applies to it.

        The same reason there is no ``--corpus`` and no ``--platform``: what the caller
        names is the finding and the worktree, and what gets checked is decided here.
        """
        assert "--skip-test-rows" not in mod._build_parser().format_help()

    def test_a_flow_row_is_still_never_run(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """Running ``test`` rows must not have widened into running every kind.

        The witness is a file the row's command would create. A ``flow`` row is
        untrusted text -- a JSON file anyone who can edit it could write -- so running
        one as argv would turn a file edit into a command with the operator's access.
        """
        witness = worktree / "witness.txt"
        a_golden_path(staged, kind="flow", command=f"touch {witness}")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, tmp_path / "findings.db", worktree)
        assert result.returncode == EXIT_HOLDS, result.stderr
        assert not witness.exists(), "a flow row was executed"
        assert [row["kind"] for row in payload(result)["needs_human"]] == ["flow"]

    def test_every_kind_at_once_counts_only_what_was_checked(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        a_behaviour_file(worktree, "test/test_behaviour.py", PASSING_TEST)
        a_golden_path(staged, kind="test", command="test/test_behaviour.py")
        a_golden_path(staged, kind="flow", command="monitor_start")
        a_golden_path(staged, kind="cron", command="a daily zizmor scan")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, tmp_path / "findings.db", worktree)
        assert result.returncode == EXIT_HOLDS, result.stderr
        body = payload(result)
        assert body["corpus_rows"] == 3
        assert body["golden_paths_checked"] == 1
        assert sorted(row["kind"] for row in body["needs_human"]) == ["cron", "flow"]

    def test_the_checked_kinds_are_declared_once(self, mod) -> None:
        """The needs-a-human split reads this tuple, so a kind cannot be both."""
        assert mod.CHECKED_KINDS == (mod.CHECKED_KIND, mod.TEST_KIND)
        assert mod.TEST_KIND == "test"


class TestTheContractCannotSteerTheGateThatReadsIt:
    """The contract file sits in the worktree under review, so the fixer can edit it.

    Nothing here pretends to be a fence against a dishonest fixer -- the design note
    in the script says so -- but three specific edits WOULD have turned the gate off
    silently, and each one is closed: naming a base that empties the diff, deleting
    the file, and leaving a contract that describes some other dispatch.
    """

    def test_the_contract_may_not_choose_the_base_it_is_judged_against(
        self, staged: Path, tmp_path: Path
    ) -> None:
        """``"base": "HEAD"`` would make the judged diff empty and every fix honoured."""
        worktree = build_repo(tmp_path, "own-base", committed=("src/kiro_crew/sandbox.py",))
        held = held_contract(tmp_path, "own-base", {**A_CONTRACT, "base": "HEAD"})
        a_golden_path(staged, kind="flow", command="monitor_start")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(
            staged, tmp_path / "findings.db", worktree, extra=["--contract", str(held)]
        )
        assert result.returncode == EXIT_BROKEN, result.stdout
        assert "src/kiro_crew/sandbox.py" in payload(result)["contract"]["why"]

    def test_a_relative_contract_path_is_resolved_before_it_is_used(
        self, staged: Path, tmp_path: Path
    ) -> None:
        """The child runs with ``cwd`` inside the worktree, so a relative path is a trap.

        A fixer-owned file at the same relative name would be enforced instead of the
        conductor's. Run from a directory that is NOT the worktree, with a decoy in the
        worktree at that same relative path, and the narrow external copy must still win.
        """
        worktree = build_repo(tmp_path, "relative", committed=("src/kiro_crew/sandbox.py",))
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "held.json").write_text(json.dumps(A_CONTRACT), encoding="utf-8")
        # The decoy: same relative name, inside the worktree, wide open.
        (worktree / "held.json").write_text(
            json.dumps({**A_CONTRACT, "allowed_paths": ["src/"], "forbidden_paths": []}),
            encoding="utf-8",
        )
        a_golden_path(staged, kind="flow", command="monitor_start")
        install_verifier(staged, VERIFIER_REJECTED)
        # The child's own cwd is what the relative name resolves against, so it is
        # passed to the child rather than set on the test worker -- a chdir there
        # is process-wide and outlives a failing assertion.
        result = run_fix(
            staged,
            tmp_path / "findings.db",
            worktree,
            extra=["--contract", "held.json"],
            cwd=outside,
        )
        assert result.returncode == EXIT_BROKEN, result.stdout
        body = payload(result)
        assert "src/kiro_crew/sandbox.py" in body["contract"]["why"]
        assert body["contract"]["report"]["contract_path"] == str(outside / "held.json")

    def test_the_base_is_passed_explicitly_on_every_invocation(self, mod) -> None:
        assert mod.CONTRACT_BASE == "origin/main"
        source = VERIFY_FIX.read_text(encoding="utf-8")
        assert '"--base",\n        CONTRACT_BASE,' in source

    def test_the_conductors_own_copy_is_what_gets_enforced(
        self, staged: Path, tmp_path: Path
    ) -> None:
        """The declaration is a file the fixer cannot reach.

        The worktree's own copy is deliberately WIDE here and the verdict still comes
        from the conductor's narrow one, which is the property -- a fixer steered by the
        code it audits can widen the file it owns.
        """
        worktree = build_repo(
            tmp_path,
            "external",
            committed=("src/kiro_crew/sandbox.py",),
            contract={**A_CONTRACT, "allowed_paths": ["src/"], "forbidden_paths": []},
        )
        held = tmp_path / "conductor" / "fix-contract.json"
        held.parent.mkdir(parents=True)
        held.write_text(json.dumps(A_CONTRACT), encoding="utf-8")
        a_golden_path(staged, kind="flow", command="monitor_start")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(
            staged, tmp_path / "findings.db", worktree, extra=["--contract", str(held)]
        )
        assert result.returncode == EXIT_BROKEN, result.stdout
        body = payload(result)
        assert "src/kiro_crew/sandbox.py" in body["contract"]["why"]
        assert body["contract"]["report"]["contract_path"] == str(held)

    def test_a_stale_contract_does_not_reject_the_wrong_dispatch(
        self, staged: Path, tmp_path: Path
    ) -> None:
        """A violated contract for ANOTHER finding settles nothing about this fix.

        Reading its rejection as this one's would send a fixer to repair a scope nobody
        declared for this run, so the mismatch is checked before the exit code branches.
        """
        worktree = build_repo(tmp_path, "stale-violation", committed=("src/kiro_crew/sandbox.py",))
        held = held_contract(tmp_path, "stale-violation", {**A_CONTRACT, "finding_ids": [99]})
        a_golden_path(staged, kind="flow", command="monitor_start")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(
            staged, tmp_path / "findings.db", worktree, extra=["--contract", str(held)]
        )
        assert result.returncode == EXIT_UNVERIFIABLE, result.stdout
        assert "does not cover finding 1" in payload(result)["contract"]["why"]

    def test_a_worktree_contract_nobody_named_is_unverifiable(
        self, staged: Path, tmp_path: Path
    ) -> None:
        """THE FENCED FINDING: a scope the subject can edit is not a scope.

        The file is honoured-shaped and the fix is inside it, and the verdict is still
        20 -- because "the conductor forgot the flag" and "the fixer wrote itself a
        contract" are indistinguishable from here.
        """
        worktree = build_repo(
            tmp_path, "unnamed", committed=("src/kiro_crew/x.py",), contract=A_CONTRACT
        )
        a_golden_path(staged, kind="flow", command="monitor_start")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, tmp_path / "findings.db", worktree)
        assert result.returncode == EXIT_UNVERIFIABLE, result.stdout
        why = payload(result)["contract"]["why"]
        assert "no --contract was named" in why
        assert "the fixer can widen" in why

    def test_a_widened_worktree_copy_cannot_pass_the_gate(
        self, staged: Path, tmp_path: Path
    ) -> None:
        """A fixer that rewrites its own contract gets 20, never 0."""
        worktree = build_repo(
            tmp_path,
            "widened",
            committed=("src/kiro_crew/sandbox.py",),
            contract={**A_CONTRACT, "allowed_paths": ["src/"], "forbidden_paths": []},
        )
        a_golden_path(staged, kind="flow", command="monitor_start")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, tmp_path / "findings.db", worktree)
        assert result.returncode == EXIT_UNVERIFIABLE, result.stdout

    def test_no_flag_asks_the_gate_to_trust_the_worktrees_copy(self, mod) -> None:
        """``--require-contract`` is gone: naming a copy IS requiring one."""
        help_text = mod._build_parser().format_help()
        assert "--contract" in help_text
        assert "--require-contract" not in help_text

    def test_a_named_contract_that_is_absent_is_unverifiable_too(
        self, staged: Path, tmp_path: Path
    ) -> None:
        """Naming a copy IS the assertion that one exists, so a missing one is unsettled."""
        worktree = build_repo(tmp_path, "named-gone", committed=("src/kiro_crew/x.py",))
        a_golden_path(staged, kind="flow", command="monitor_start")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(
            staged,
            tmp_path / "findings.db",
            worktree,
            extra=["--contract", str(tmp_path / "nowhere.json")],
        )
        assert result.returncode == EXIT_UNVERIFIABLE, result.stdout
        assert "not a file" in payload(result)["contract"]["why"]

    def test_a_named_contract_for_another_finding_is_unverifiable(
        self, staged: Path, tmp_path: Path
    ) -> None:
        """A stale contract from an earlier dispatch is not this run's scope."""
        worktree = build_repo(tmp_path, "wrong-finding", committed=("src/kiro_crew/x.py",))
        held = held_contract(tmp_path, "wrong-finding", {**A_CONTRACT, "finding_ids": [99]})
        a_golden_path(staged, kind="flow", command="monitor_start")
        install_verifier(staged, VERIFIER_REJECTED)
        # ``run_fix`` verifies finding 1.
        result = run_fix(
            staged, tmp_path / "findings.db", worktree, extra=["--contract", str(held)]
        )
        assert result.returncode == EXIT_UNVERIFIABLE, result.stdout
        assert "does not cover finding 1" in payload(result)["contract"]["why"]

    def test_a_named_contract_that_does_cover_the_finding_holds(
        self, staged: Path, tmp_path: Path
    ) -> None:
        worktree = build_repo(tmp_path, "right-finding", committed=("src/kiro_crew/x.py",))
        held = held_contract(tmp_path, "right-finding", {**A_CONTRACT, "finding_ids": [1, 16]})
        a_golden_path(staged, kind="flow", command="monitor_start")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(
            staged, tmp_path / "findings.db", worktree, extra=["--contract", str(held)]
        )
        assert result.returncode == EXIT_HOLDS, result.stderr

    def test_a_mismatch_is_read_from_the_payload_not_the_file(self, mod) -> None:
        """Unit-level, so the two shapes that are NOT a wildcard are pinned."""
        assert mod.contract_finding_mismatch({"contract": {"finding_ids": [16]}}, 16) is None
        assert "does not cover finding 1" in str(
            mod.contract_finding_mismatch({"contract": {"finding_ids": [16]}}, 1)
        )
        # An absent or unreadable list is a mismatch, never a permission.
        assert mod.contract_finding_mismatch({}, 1) is not None
        assert mod.contract_finding_mismatch({"contract": {"finding_ids": []}}, 1) is not None


class TestASelectorCannotLeaveTheWorktreeOrCrashTheRun:
    """Two selector shapes that survived ``--`` and the option check."""

    def test_a_response_file_selector_is_refused(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """pytest's argparse expands ``@file`` BEFORE it honours ``--``.

        So the row is not a positional at all: the file's contents become argv, which
        can name a test outside the disposable checkout and load that tree's
        ``conftest.py`` during collection.
        """
        outside = tmp_path / "pytest-args"
        outside.write_text("--version\n", encoding="utf-8")
        a_golden_path(staged, kind="test", command=f"@{outside}")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, tmp_path / "findings.db", worktree)
        assert result.returncode == EXIT_UNVERIFIABLE, result.stdout
        assert "response file" in payload(result)["unverifiable"][0]["why"]

    def test_a_nul_bearing_selector_is_refused_rather_than_crashing(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """``Popen`` raises ``ValueError`` for a NUL argument, which is not a verdict.

        JSON carries ``\u0000`` inside a string, so a corpus row can hold one, and an
        uncaught raise is exit 1 with no payload -- outside this script's contract.
        """
        a_golden_path(staged, kind="test", command="test/test_behaviour\x00.py")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, tmp_path / "findings.db", worktree)
        assert result.returncode == EXIT_UNVERIFIABLE, result.stdout
        assert "NUL byte" in payload(result)["unverifiable"][0]["why"]
        assert "Traceback" not in result.stderr

    def test_a_nul_in_any_argv_is_a_launch_failure_not_a_traceback(self, mod, worktree) -> None:
        """The backstop under the selector check, for every child this script spawns."""
        # The VERDICT is the assertion, not the message: CPython says "embedded null
        # byte" on POSIX and "embedded null character" on Windows, so pinning either
        # spelling would make this a platform test instead of a contract one.
        outcome, code, text = mod.run_child([sys.executable, "-c", "pass\x00"], worktree, 30)
        assert outcome == "launch-failed"
        assert "null" in text.lower()

    def test_an_interrupted_or_aborted_row_is_unverifiable_not_broken(self, mod) -> None:
        """2 is an interrupted run and 3 is pytest's own internal error.

        Neither is a behaviour that failed, so reporting them as broken would send a
        fixer to repair code that nothing judged.
        """
        source = VERIFY_FIX.read_text(encoding="utf-8")
        assert "if code == PYTEST_FAILED:" in source
        assert "if code in (PYTEST_INTERRUPTED, PYTEST_INTERNAL):" in source
        assert mod.PYTEST_INTERRUPTED == 2 and mod.PYTEST_INTERNAL == 3
