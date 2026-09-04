"""Tests for the prepare-pr prove.py test-strength proof.

prove.py reverts a change's production hunks, keeps its test hunks, and re-runs
the changed test files.  These tests pin the four verdicts that matter and the
two ways the check could lie:

- A red mutated run caused only by collection errors is INCONCLUSIVE, not proof.
  A revert that removes a symbol a test imports makes every test fail and the
  exit code non-zero, which reads as proof while no assertion ran.
- An uncommitted edit to a file under proof is refused, because the verdict would
  describe committed code the caller is not looking at.

Also pins that per-hunk mode names the hunk no test catches, which whole-diff
mode cannot see.
"""

from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path

from skill_script_helpers import no_bytecode

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVE = str(
    REPO_ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "prepare-pr"
    / "scripts"
    / "prove.py"
)


def _load_prove():
    """Import prove.py as a module for unit-level assertions on its helpers.

    Bytecode writing is disabled for the duration: prove.py lives inside the
    checked-out skill tree, so a plain import would leave a ``__pycache__``
    directory in the working copy -- a persistent side effect outside any tmp
    dir, which the blocking no-test-side-effects rule forbids.
    """
    sys.path.insert(0, str(Path(PROVE).parent))
    try:
        with no_bytecode():
            return importlib.import_module("prove")
    finally:
        sys.path.pop(0)


def _load_reporter_plugin(tmp_path: Path):
    """Load the generated reporter plugin the way prove.py makes pytest load it.

    prove.py writes _REPORTER_PLUGIN to ``_prove_reporter.py`` and passes it to
    pytest with ``-p``, so importing it from a file here exercises the real load
    path rather than a synthetic namespace.
    """
    src = _load_prove()._REPORTER_PLUGIN
    path = tmp_path / "_prove_reporter.py"
    path.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("_prove_reporter_undertest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXIT_PROVEN = 0
EXIT_NOTHING_TO_PROVE = 10
EXIT_NOT_PROVEN = 20
EXIT_INCONCLUSIVE = 21
EXIT_BASELINE_RED = 30
EXIT_ENV = 2


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    """A git repo on branch main with one production module and one test file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "prove@test")
    _git(repo, "config", "user.name", "prove")
    (repo / "mod.py").write_text("def last_char(s):\n    return s[len(s) - 2]\n")
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\ndef test_smoke():\n    assert last_char('abc') == 'b'\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    return repo


def _run(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, PROVE, "--base", "main", *extra],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )


def test_a_test_that_catches_the_reintroduced_bug_is_proven(tmp_path: Path) -> None:
    """Also pins the stale-bytecode guard.

    The reverted line (``- 1`` to ``- 2``) is the SAME byte length as the
    original, which is half of CPython's (mtime, size) ``.pyc`` invalidation key.
    Without ``PYTHONDONTWRITEBYTECODE`` the baseline run's cached bytecode is
    reused when the revert lands in the same mtime tick, the mutation never
    reaches the interpreter, and this strong test reports NOT_PROVEN.  Keep the
    lengths equal.
    """
    repo = _repo(tmp_path)
    # Fix the off-by-one, and assert the boundary the fix changes.
    (repo / "mod.py").write_text("def last_char(s):\n    return s[len(s) - 1]\n")
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\ndef test_last():\n    assert last_char('abc') == 'c'\n"
    )
    _git(repo, "commit", "-aqm", "fix off-by-one")

    r = _run(repo)
    assert r.returncode == EXIT_PROVEN, r.stdout + r.stderr
    assert "PROVEN" in r.stdout


def test_a_test_that_passes_with_the_bug_back_is_not_proven(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    # Same real fix, but the test asserts something the fix does not affect, so it
    # stays green once the bug is reintroduced.
    (repo / "mod.py").write_text("def last_char(s):\n    return s[len(s) - 1]\n")
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\ndef test_returns_a_str():\n"
        "    assert isinstance(last_char('abc'), str)\n"
    )
    _git(repo, "commit", "-aqm", "fix off-by-one with a weak test")

    r = _run(repo)
    assert r.returncode == EXIT_NOT_PROVEN, r.stdout + r.stderr
    assert "NOT_PROVEN" in r.stdout


def test_collection_errors_alone_are_inconclusive_not_proof(tmp_path: Path) -> None:
    """The guard against reading a broken revert as evidence.

    Here the change ADDS the symbol the test imports, so reverting it makes the
    test module fail to import.  Every test errors at collection and pytest exits
    non-zero -- the shape that looks like proof.  No assertion ever ran, so the
    verdict must not be PROVEN.
    """
    repo = _repo(tmp_path)
    (repo / "mod.py").write_text(
        "def last_char(s):\n    return s[len(s) - 1]\n\n\ndef first_char(s):\n    return s[0]\n"
    )
    (repo / "test_mod.py").write_text(
        "from mod import first_char\n\n\ndef test_first():\n    assert first_char('abc') == 'a'\n"
    )
    _git(repo, "commit", "-aqm", "add first_char")

    r = _run(repo)
    assert r.returncode == EXIT_INCONCLUSIVE, r.stdout + r.stderr
    assert "INCONCLUSIVE" in r.stdout
    assert "PROVEN:" not in r.stdout


def test_production_only_change_has_nothing_to_prove(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "mod.py").write_text("def last_char(s):\n    return s[len(s) - 1]\n")
    _git(repo, "commit", "-aqm", "fix with no test")

    r = _run(repo)
    assert r.returncode == EXIT_NOTHING_TO_PROVE, r.stdout + r.stderr


def test_test_only_change_has_nothing_to_prove(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\ndef test_smoke():\n"
        "    assert last_char('abc') == 'b'\n\n\ndef test_more():\n"
        "    assert last_char('xy') == 'x'\n"
    )
    _git(repo, "commit", "-aqm", "more tests only")

    r = _run(repo)
    assert r.returncode == EXIT_NOTHING_TO_PROVE, r.stdout + r.stderr


def test_a_red_baseline_is_refused_before_mutating(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "mod.py").write_text("def last_char(s):\n    return s[len(s) - 1]\n")
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\ndef test_wrong():\n    assert last_char('abc') == 'z'\n"
    )
    _git(repo, "commit", "-aqm", "fix plus an already-failing test")

    r = _run(repo)
    assert r.returncode == EXIT_BASELINE_RED, r.stdout + r.stderr


def test_uncommitted_edits_in_files_under_proof_are_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "mod.py").write_text("def last_char(s):\n    return s[len(s) - 1]\n")
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\ndef test_last():\n    assert last_char('abc') == 'c'\n"
    )
    _git(repo, "commit", "-aqm", "fix off-by-one")
    # Working-tree edit the proof would silently ignore.
    (repo / "mod.py").write_text("def last_char(s):\n    return 'zzz'\n")

    r = _run(repo)
    assert r.returncode == EXIT_ENV, r.stdout + r.stderr
    assert "uncommitted" in r.stderr


def test_per_hunk_names_the_hunk_no_test_catches(tmp_path: Path) -> None:
    """Whole-diff mode cannot see a partially-tested change; per-hunk can.

    Both buggy functions must exist on the BASE commit so the feature diff is two
    separate hunks in one file.  Introducing the second function on the branch
    would make the diff one large hunk and there would be nothing to attribute.
    """
    repo = tmp_path / "two"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "prove@test")
    _git(repo, "config", "user.name", "prove")
    # Two independent off-by-ones, far enough apart to stay separate hunks.
    (repo / "mod.py").write_text(
        "def last_char(s):\n    return s[len(s) - 2]\n"
        + "\n" * 12
        + "def second_char(s):\n    return s[2]\n"
    )
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\ndef test_smoke():\n    assert last_char('abc') == 'b'\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "two bugs")
    _git(repo, "checkout", "-q", "-b", "feature")

    (repo / "mod.py").write_text(
        "def last_char(s):\n    return s[len(s) - 1]\n"
        + "\n" * 12
        + "def second_char(s):\n    return s[1]\n"
    )
    # Only the first fix is covered.
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\ndef test_last():\n    assert last_char('abc') == 'c'\n"
    )
    _git(repo, "commit", "-aqm", "fix both, test one")

    r = _run(repo, "--per-hunk")
    assert r.returncode == EXIT_NOT_PROVEN, r.stdout + r.stderr
    assert "uncaught: mod.py:" in r.stdout


def test_split_hunks_repeats_the_file_header_for_each_hunk() -> None:
    """Each emitted patch must carry its file header or git apply cannot place it."""
    prove = _load_prove()

    patch = (
        "diff --git a/mod.py b/mod.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-a\n"
        "+b\n"
        "@@ -20,2 +20,2 @@\n"
        "-c\n"
        "+d\n"
    )
    hunks = prove.split_hunks(patch)
    assert [label for label, _ in hunks] == ["mod.py:1", "mod.py:20"]
    for _, text in hunks:
        assert text.startswith("diff --git a/mod.py b/mod.py")
        assert text.count("@@ -") == 1


def test_pytest_runs_with_bytecode_writing_disabled(monkeypatch, tmp_path: Path) -> None:
    """Pins the stale-bytecode guard by contract rather than by racing it.

    The bug it prevents only fires when a revert lands in the same filesystem
    mtime tick as the baseline run, so an end-to-end test cannot reliably observe
    it -- removing the guard leaves the suite green most of the time.  Asserting
    the interpreter is launched with bytecode writing off is deterministic and
    fails the moment the guard is dropped.
    """
    prove = _load_prove()
    seen: dict = {}

    class _Done:
        returncode = 0

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return _Done()

    monkeypatch.setattr(prove.subprocess, "run", fake_run)
    prove.run_pytest(Path("/nonexistent"), ["test_mod.py"], tmp_path / "x.json")

    assert seen["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_a_non_python_production_file_is_still_reverted(tmp_path: Path) -> None:
    """A mixed change must revert its config hunk, not just its Python.

    Restricting production to ``.py`` would leave the config standing during the
    mutation, so a change whose behaviour lives in JSON could report PROVEN on a
    mutation that never touched the thing under test.
    """
    repo = tmp_path / "mixed"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "prove@test")
    _git(repo, "config", "user.name", "prove")
    (repo / "limits.json").write_text('{"max": 1}\n')
    (repo / "mod.py").write_text(
        "import json\n\n\ndef limit():\n" "    return json.load(open('limits.json'))['max']\n"
    )
    (repo / "test_mod.py").write_text(
        "from mod import limit\n\n\ndef test_one():\n    assert limit() == 1\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    # The whole behaviour change lives in the JSON.
    (repo / "limits.json").write_text('{"max": 9}\n')
    (repo / "test_mod.py").write_text(
        "from mod import limit\n\n\ndef test_nine():\n    assert limit() == 9\n"
    )
    _git(repo, "commit", "-aqm", "raise the limit")

    r = _run(repo)
    assert r.returncode == EXIT_PROVEN, r.stdout + r.stderr


def test_an_unexpectedly_passing_strict_xfail_is_not_proof(tmp_path: Path) -> None:
    """A strict xfail that flips to pass is reported failed at phase call.

    Nothing asserted the reverted bug in that case, so counting it would
    manufacture proof out of an xfail flip.
    """
    plugin = _load_reporter_plugin(tmp_path)

    class _Report:
        when = "call"
        outcome = "failed"
        wasxfail = "reason: strict xfail passed"
        failed = True

    plugin.pytest_runtest_logreport(_Report())
    assert plugin._counts["failures"] == 0
    assert plugin._counts["tests_run"] == 1


def test_an_option_like_base_cannot_suppress_the_diff(tmp_path: Path) -> None:
    """A ``--base`` that looks like a git option must not fake NOTHING_TO_PROVE.

    Without ``--`` in the merge-base arguments, git parses ``--octopus`` as an
    option, merge-base returns HEAD, and the resulting empty diff reports
    NOTHING_TO_PROVE for a change that has plenty to prove — a silent false
    negative in the one tool whose job is to catch silent false negatives.
    """
    repo = tmp_path / "inject"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "prove@test")
    _git(repo, "config", "user.name", "prove")
    (repo / "mod.py").write_text("def bump(n):\n    return n - 1\n")
    (repo / "test_mod.py").write_text(
        "from mod import bump\n\n\ndef test_bump():\n    assert bump(3) == 2\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "mod.py").write_text("def bump(n):\n    return n + 1\n")
    (repo / "test_mod.py").write_text(
        "from mod import bump\n\n\ndef test_bump():\n    assert bump(3) == 4\n"
    )
    _git(repo, "commit", "-aqm", "fix the sign")

    r = _run(repo, "--base=--octopus")
    # The verdict must not be NOTHING_TO_PROVE: either the ref is rejected
    # (EXIT_ENV) or it resolves normally, but the diff is never silently empty.
    assert r.returncode != EXIT_NOTHING_TO_PROVE, r.stdout + r.stderr


def test_importing_prove_leaves_no_bytecode_in_the_checkout() -> None:
    """``_load_prove`` must not write ``__pycache__`` into the skill tree.

    prove.py sits in the checked-out source tree, so an ordinary import drops a
    .pyc beside it that outlives the run -- the same bytecode pollution the
    script itself guards against when it launches pytest.

    The two lines of setup are load-bearing, and both come from this test having
    failed the release gate. Asserting only that the file is ABSENT measures the
    ambient filesystem rather than this loader: the gate runs all 59k tests in
    one process tree, and anything else that imports from that directory leaves
    residue this would then blame on ``_load_prove``. And because ``sys.modules``
    caches ``prove``, a run where something imported it first makes
    ``_load_prove`` a no-op -- the assertion then passes while exercising
    nothing, which is the worse of the two failure directions. Evicting the
    module and removing the cache file first makes the import real and the
    verdict this loader's own.

    Removing it is net-zero, and that matters: the same no-test-side-effects rule
    this test enforces also binds the test. The file is restored byte-for-byte on
    every exit path, so a working copy that had a stale .pyc still has it
    afterwards, and one that did not is not given one by a failing assertion.
    """
    cache = Path(importlib.util.cache_from_source(PROVE))
    existing = cache.read_bytes() if cache.exists() else None
    sys.modules.pop("prove", None)
    cache.unlink(missing_ok=True)
    try:
        module = _load_prove()

        cached = getattr(module, "__cached__", None)
        assert cached, "expected __cached__ to be set"
        assert Path(cached) == cache, f"cache path moved: {cached} is not {cache}"
        assert not cache.exists(), f"import wrote bytecode to {cached}"
    finally:
        if existing is None:
            cache.unlink(missing_ok=True)
        else:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(existing)


def test_a_call_phase_exception_is_not_proof(tmp_path: Path) -> None:
    """Reverting production can break the call itself; that is not an assertion.

    Removing a method makes the test raise AttributeError mid-call. The run is
    red and no assertion ever ran, so counting it would be the same false-proof
    the collection-error case already guards against.
    """
    repo = tmp_path / "raises"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "prove@test")
    _git(repo, "config", "user.name", "prove")
    (repo / "mod.py").write_text("class Thing:\n    pass\n")
    (repo / "test_mod.py").write_text(
        "from mod import Thing\n\n\ndef test_exists():\n    assert Thing() is not None\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "mod.py").write_text("class Thing:\n    def bump(self):\n        return 1\n")
    (repo / "test_mod.py").write_text(
        "from mod import Thing\n\n\ndef test_bump():\n    assert Thing().bump() == 1\n"
    )
    _git(repo, "commit", "-aqm", "add bump")

    r = _run(repo)
    assert r.returncode == EXIT_INCONCLUSIVE, r.stdout + r.stderr


def test_an_unmet_pytest_raises_still_counts_as_proof(tmp_path: Path) -> None:
    """pytest.fail / an unmet pytest.raises is a deliberate test judgement.

    Both surface as Failed rather than AssertionError, so the assertion filter
    must admit them or the common raises-based bug test would read INCONCLUSIVE.
    """
    repo = tmp_path / "guard"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "prove@test")
    _git(repo, "config", "user.name", "prove")
    (repo / "mod.py").write_text("def check(n):\n    return n\n")
    (repo / "test_mod.py").write_text(
        "from mod import check\n\n\ndef test_ok():\n    assert check(1) == 1\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    # The fix adds validation; the test proves it with pytest.raises.
    (repo / "mod.py").write_text(
        "def check(n):\n    if n < 0:\n        raise ValueError('negative')\n    return n\n"
    )
    (repo / "test_mod.py").write_text(
        "import pytest\n\nfrom mod import check\n\n\n"
        "def test_rejects_negative():\n"
        "    with pytest.raises(ValueError):\n        check(-1)\n"
    )
    _git(repo, "commit", "-aqm", "reject negatives")

    r = _run(repo)
    assert r.returncode == EXIT_PROVEN, r.stdout + r.stderr


def test_a_doc_hunk_does_not_drive_not_proven(tmp_path: Path) -> None:
    """Prose is unfalsifiable, so --per-hunk must not call it uncaught.

    This repo mandates same-commit spec updates, so treating a doc hunk as an
    uncaught production hunk would make attribution mode red on every compliant
    change and bury the real uncaught code hunks.
    """
    repo = tmp_path / "docs"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "prove@test")
    _git(repo, "config", "user.name", "prove")
    (repo / "mod.py").write_text("def bump(n):\n    return n - 1\n")
    (repo / "README.md").write_text("bump decrements\n")
    (repo / "test_mod.py").write_text(
        "from mod import bump\n\n\ndef test_bump():\n    assert bump(3) == 2\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "mod.py").write_text("def bump(n):\n    return n + 1\n")
    (repo / "README.md").write_text("bump increments\n")
    (repo / "test_mod.py").write_text(
        "from mod import bump\n\n\ndef test_bump():\n    assert bump(3) == 4\n"
    )
    _git(repo, "commit", "-aqm", "fix sign and doc")

    r = _run(repo, "--per-hunk")
    assert r.returncode == EXIT_PROVEN, r.stdout + r.stderr
    # Reported, but as its own bucket -- never as an uncaught production hunk.
    assert "unprovable: README.md" in r.stdout, r.stdout
    assert "uncaught" not in r.stdout, r.stdout
