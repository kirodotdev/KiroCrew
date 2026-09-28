"""``verify_fix.py`` — the three-step gate, and the verdict it must never reach.

The properties pinned here are the ones a prompt cannot hold. A fix is verified
only when it stayed inside the scope it was dispatched with, the proof stops
reproducing, AND every legitimate operation still works, so the interesting
assertions are all about the verdict LADDER: a proof that still reproduces outranks
everything, a broken golden path is a rejection, and ``holds`` is unreachable while
any golden path went unchecked.

Two of those steps exist because of one round. A fix for a cron seam landed as an
addition to ``sandbox._AGENT_DENIED_ENV_KEYS``, which stripped the OPERATOR's own
``KIROCREW_SECURITY_POLICY`` from every agent child. The proof stopped reproducing,
every ``shell`` row stayed permitted — none of them is a bash command that notices
an env var going missing — and this gate exited 0. So a fix is now also judged
against the blast radius the conductor declared in ``fix-contract.json``
(``TestTheFixContractIsStepZero``), and a golden path may be a BEHAVIOUR run as a
pytest selector rather than a command line (``TestTheTestKindIsRunAgainstTheFix``).

The script is driven as a SUBPROCESS with its siblings staged beside it, because
that is the contract that matters: it finds ``verify_finding.py`` and ``ledger.py``
as files next to itself and the committed corpus one directory up, so a property
that holds only when the module is imported into the test process would not be the
property the harness relies on. Staging the directory is also what lets each test
state the verifier's exit status and write the corpus at its own call site, and
what makes the broken-install case reachable without breaking the checkout.

The deny fence is reached the way the shipped probe reaches it: the probe puts the
worktree's ``src`` at the head of ``PYTHONPATH`` and imports ``kiro_crew.security``
from there, so a test stages a fake module at that path -- one that refuses any
command carrying a marker, or one that fails to import. There is deliberately no
flag that substitutes a classifier PROGRAM: that would decode caller text into
subprocess argv, which is arbitrary execution outside the tool gate. One class at
the end does use the REAL fence, and asserts the shipped corpus against it -- that
is the corpus's whole purpose, and a test that stubbed it would assert nothing about
the rows.

The other property with its own class is that NOTHING out of the corpus is
executed. A ``flow`` or ``cron`` row is untrusted text -- a JSON file anyone can
edit, and a ledger whose CLI is not an authentication boundary -- so running one
would turn a file edit into a command with the operator's access. Those tests plant
a witness file a row would create if it ran, and assert it never appears.

And the gate reads the COMMITTED FILE, not the ledger's ``golden_paths`` table: the
RFC rules that "both gates read the file and nothing else". One class pins it from
both sides -- a row that is only in the ledger does not gate, and a row retired in
the ledger still does -- because a gate that read the table could be steered by a
row flip into passing the fix that broke it. The same class pins that no argument
names another corpus or another platform, and that the fence the probe classified
against is the worktree's own rather than an installed package it fell through to.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script
from test_security_conductor_fix_contract import build_repo

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "src" / "kiro_crew" / "builtin_skills" / "security-conductor"
SCRIPTS = SKILL_DIR / "scripts"
VERIFY_FIX = SCRIPTS / "verify_fix.py"
LEDGER = SCRIPTS / "ledger.py"
CHECK_FIX_CONTRACT = SCRIPTS / "check_fix_contract.py"
CORPUS = SKILL_DIR / "golden-paths.json"
CORPUS_FILENAME = CORPUS.name

#: The platform the script derives for THIS host, and the one it must ignore. The
#: filter is exercised on whichever host runs the suite, which is the point: a
#: Windows lane skipping the posix rows is the same property as the reverse.
HOST = "windows" if os.name == "nt" else "posix"
OTHER_HOST = "posix" if HOST == "windows" else "windows"

EXIT_HOLDS = 0
EXIT_REPRODUCES = 10
EXIT_UNVERIFIABLE = 20
EXIT_BROKEN = 30
EXIT_INVALID = 2

#: The verifier's own contract, as ``verify_fix.py`` reads it.
VERIFIER_CONFIRMED = 0
VERIFIER_REJECTED = 10
VERIFIER_NEEDS_HUMAN = 20

#: A stub verifier: it takes the flags the real one takes, ignores them, and exits
#: the status the test asked for. The real script's judgement is not what these
#: tests are about -- the fold over its exit code is.
STUB_VERIFIER = """import sys
sys.exit({code})
"""

#: A fake ``kiro_crew.security`` staged on the worktree's ``src``, where the probe
#: imports the fence from. It refuses any command containing a marker substring, so
#: a test can arrange exactly one broken golden path without depending on what the
#: real fence happens to think.
FAKE_FENCE = """MARKER = {marker!r}
TIER = {tier!r}
OMIT = {omit!r}


def _refuses(command):
    return MARKER is not None and MARKER in command


def is_sensitive_bash_command(command, *args, **kwargs):
    if TIER == "is_sensitive_bash_command" and _refuses(command):
        return "stub sensitive: %s" % MARKER
    return None


def audit_bash_exfiltration(command, *args, **kwargs):
    if TIER == "audit_bash_exfiltration" and _refuses(command):
        return "stub exfil: %s" % MARKER
    return None


def is_denied(command, *args, **kwargs):
    if TIER == "is_denied" and _refuses(command):
        return "stub refusal: %s" % MARKER
    return None


if OMIT:
    del globals()[OMIT]
"""

#: A fake fence that cannot be imported, which is what an uninstallable package
#: looks like to the probe.
FAKE_FENCE_UNAVAILABLE = """raise ImportError("stub: not importable")
"""


@pytest.fixture
def mod():
    return load_skill_script("security_conductor_verify_fix", VERIFY_FIX)


@pytest.fixture
def ledger_mod():
    return load_skill_script("security_conductor_ledger_for_fix_tests", LEDGER)


@pytest.fixture
def staged(tmp_path: Path) -> Path:
    """A scripts directory holding the script, its ledger, and the contract sibling.

    The verifier is absent on purpose: every test that wants one installs it with a
    chosen exit status, so the exit code under test is always stated at the call
    site rather than inherited from whatever the real verifier decides. The
    broken-install case then needs no special setup at all. The corpus is absent for
    the same reason: :func:`a_golden_path` writes it one directory up, where the
    script looks for the committed export beside the skill.
    """
    directory = tmp_path / "scripts"
    directory.mkdir()
    shutil.copy2(VERIFY_FIX, directory / "verify_fix.py")
    shutil.copy2(LEDGER, directory / "ledger.py")
    # The contract sibling is staged rather than absent because it is invoked only
    # when the worktree carries a ``fix-contract.json``, so its presence changes
    # nothing for the tests that write no contract -- and the broken-install case
    # unlinks it at its own call site.
    shutil.copy2(CHECK_FIX_CONTRACT, directory / "check_fix_contract.py")
    return directory


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A target that passes the checkout screen.

    A bare ``.git`` directory is enough because that IS the screen -- the same one
    ``verify_finding.py`` applies -- and standing up a real repository per test
    would pay git's startup cost dozens of times to assert nothing extra.
    """
    directory = tmp_path / "target"
    directory.mkdir()
    (directory / ".git").mkdir()
    return directory


def install_verifier(staged: Path, code: int) -> None:
    (staged / "verify_finding.py").write_text(STUB_VERIFIER.format(code=code), encoding="utf-8")


def fence(
    marker: str | None = None,
    *,
    available: bool = True,
    tier: str = "is_denied",
    omit: str | None = None,
) -> dict:
    """What fake fence a test wants staged; :func:`run_fix` writes it.

    ``tier`` names which of the three checks refuses the marker; ``omit`` deletes one
    check from the fake module, which is what a tree missing a tier looks like.
    """
    return {"marker": marker, "available": available, "tier": tier, "omit": omit}


def stage_fence(worktree: Path, spec: dict) -> None:
    """Write a fake ``kiro_crew.security`` where the probe will import it.

    ``<worktree>/src`` leads the probe's ``PYTHONPATH``, so a package there shadows
    the installed one for the child only -- the test process keeps the real fence
    for the corpus class below.
    """
    package = worktree / "src" / "kiro_crew"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    body = (
        FAKE_FENCE.format(marker=spec["marker"], tier=spec["tier"], omit=spec["omit"])
        if spec["available"]
        else FAKE_FENCE_UNAVAILABLE
    )
    (package / "security.py").write_text(body, encoding="utf-8")


def corpus_path(staged: Path) -> Path:
    """Where the staged script finds its committed export: beside the skill."""
    return staged.parent / CORPUS_FILENAME


def a_golden_path(
    staged: Path,
    *,
    kind: str = "shell",
    command: str,
    platform: str = "any",
) -> int:
    """Append one row to the staged corpus file; returns its ``entry`` index."""
    path = corpus_path(staged)
    rows = json.loads(path.read_text(encoding="utf-8"))["golden_paths"] if path.exists() else []
    rows.append(
        {
            "kind": kind,
            "surface": "test",
            "command_or_flow": command,
            "platform": platform,
            "reason": "a legitimate operation this fix must keep alive",
        }
    )
    path.write_text(json.dumps({"golden_paths": rows}), encoding="utf-8")
    return len(rows) - 1


def a_ledger_golden_path(ledger_mod, db: Path, *, command: str, active: bool = True) -> int:
    """An approved row in the LEDGER's table, which the gate must not consult.

    Written the way the CLI writes one -- proposed, then approved -- and retired
    the way the RFC retires one, by the hand ``UPDATE``.
    """
    conn = ledger_mod.connect(db)
    try:
        ledger_mod.init_schema(conn)
        path_id, _ = ledger_mod.propose_golden_path(
            conn,
            kind="shell",
            surface="test",
            command_or_flow=command,
            platform="any",
            reason="a row the ledger holds",
            source_finding_id=None,
        )
        ledger_mod.approve_golden_path(conn, path_id=path_id, approved_by="tester")
        if not active:
            with conn:
                conn.execute("UPDATE golden_paths SET active = 0 WHERE id = ?", (path_id,))
    finally:
        conn.close()
    return path_id


def run_fix(
    staged: Path,
    db: Path | None,
    worktree: Path,
    *,
    fence: dict | None = None,
    timeout: int = 30,
    env: dict[str, str] | None = None,
    extra: list[str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Drive the staged ``verify_fix.py`` as the conductor does: a child process.

    ``cwd`` defaults to the worktree -- the directory the conductor launches the
    gate from -- and is never the checkout this test process inherited. A test
    that is ABOUT where a relative argument resolves passes its own.
    """
    argv = [
        sys.executable,
        str(staged / "verify_fix.py"),
        "--finding-id",
        "1",
        "--worktree",
        str(worktree),
        "--timeout",
        str(timeout),
    ]
    if db is not None:
        argv[2:2] = ["--db", str(db)]
    argv.extend(extra or [])
    if fence is not None:
        stage_fence(worktree, fence)
    child = dict(os.environ)
    if env:
        child.update(env)
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        env=child,
        cwd=str(cwd or worktree),
    )


def payload(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestTheProofIsTheFirstGate:
    def test_a_reproducing_proof_is_ten_and_outranks_everything(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """A fix that did not land is not a fix whose golden paths are interesting.

        The refused golden path is present deliberately: the verdict must still be
        10, because reporting 30 would send the reviewer to fix a legitimate
        operation while the vulnerability is still open.
        """
        db = tmp_path / "findings.db"
        a_golden_path(staged, command="gh pr view 1 --json state REFUSE-ME")
        install_verifier(staged, VERIFIER_CONFIRMED)
        result = run_fix(staged, db, worktree, fence=fence("REFUSE-ME"))
        assert result.returncode == EXIT_REPRODUCES, result.stderr
        body = payload(result)
        assert body["verdict"] == "reproduces"
        # The broken row is still REPORTED, so one round of feedback carries both.
        assert [row["entry"] for row in body["broken"]]

    def test_a_rejected_proof_with_intact_golden_paths_holds(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        db = tmp_path / "findings.db"
        a_golden_path(staged, command="gh pr view 1 --json state")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, fence=fence("REFUSE-ME"))
        assert result.returncode == EXIT_HOLDS, result.stderr
        body = payload(result)
        assert body["verdict"] == "holds"
        assert body["broken"] == []
        assert body["unverifiable"] == []

    def test_a_verifier_that_cannot_settle_it_is_twenty(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        db = tmp_path / "findings.db"
        a_golden_path(staged, command="git status --porcelain")
        install_verifier(staged, VERIFIER_NEEDS_HUMAN)
        result = run_fix(staged, db, worktree, fence=fence())
        assert result.returncode == EXIT_UNVERIFIABLE, result.stderr
        assert payload(result)["poc"]["verdict"] == "unverifiable"

    def test_an_exit_status_outside_the_contract_is_twenty(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """An unrecognised status is not permission.

        The verifier's contract names 0, 10, 20 and 2. A 7 means this script is
        reading a version of it that it does not understand, and the only safe
        reading of that is that nothing was settled.
        """
        db = tmp_path / "findings.db"
        install_verifier(staged, 7)
        result = run_fix(staged, db, worktree, fence=fence())
        assert result.returncode == EXIT_UNVERIFIABLE, result.stderr
        assert "not in its contract" in payload(result)["poc"]["reason"]


class TestAnAbsentVerifierIsNeverAPass:
    """The sibling ships in the same bundle, so its absence is a broken install.

    A broken install is not a stage of a rollout, and it is still the case the whole
    exit ladder exists for: with no verifier there is no evidence the fix landed, and
    a 0 here would report an unverified fix as verified.
    """

    def test_a_missing_verifier_is_twenty_and_names_the_broken_install(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        db = tmp_path / "findings.db"
        assert not (staged / "verify_finding.py").exists()
        result = run_fix(staged, db, worktree, fence=fence())
        assert result.returncode == EXIT_UNVERIFIABLE, result.stderr
        body = payload(result)
        assert body["verdict"] == "unverifiable"
        assert "could not be invoked" in body["poc"]["reason"]
        assert "broken installation" in body["poc"]["reason"]

    def test_a_missing_verifier_is_not_read_as_a_rejected_argument(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """Exit 2 is the verifier's code for "I rejected your input", and the
        interpreter would produce it for a nonexistent script argument. Letting the
        spawn answer would turn a broken install into a lie about the caller's
        arguments -- and into exit 2, which is not a verdict about the fix at all."""
        db = tmp_path / "findings.db"
        result = run_fix(staged, db, worktree, fence=fence())
        assert result.returncode != EXIT_INVALID
        assert "rejected its input" not in payload(result)["poc"]["reason"]


class TestAnUnloadableLedgerIsNeverAPassOrACrash:
    """``ledger.py`` ships beside this script; one that will not load is a broken
    install, and a broken install is exit 20 with the payload every other path
    prints -- not a traceback and exit 1, which is no verdict in the contract."""

    def test_a_missing_ledger_is_twenty_and_names_the_broken_install(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        (staged / "ledger.py").unlink()
        install_verifier(staged, VERIFIER_REJECTED)
        a_golden_path(staged, command="git status --porcelain")
        result = run_fix(staged, tmp_path / "findings.db", worktree, fence=fence())
        assert result.returncode == EXIT_UNVERIFIABLE, result.stderr
        assert "Traceback" not in result.stderr
        body = payload(result)
        assert body["verdict"] == "unverifiable"
        assert body["golden_paths_checked"] == 0
        assert "ledger.py could not be loaded" in body["poc"]["reason"]
        assert "broken installation" in body["poc"]["reason"]
        assert body["corpus_problems"] == [body["poc"]["reason"]]
        assert "unverifiable: ledger.py could not be loaded" in result.stderr

    def test_a_ledger_that_does_not_import_is_twenty_not_a_traceback(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """A corrupt file is the other way the same sibling fails to load."""
        (staged / "ledger.py").write_text("def broken(:\n", encoding="utf-8")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, tmp_path / "findings.db", worktree, fence=fence())
        assert result.returncode == EXIT_UNVERIFIABLE, result.stderr
        assert "Traceback" not in result.stderr
        body = payload(result)
        assert body["verdict"] == "unverifiable"
        assert "SyntaxError" in body["poc"]["reason"]

    def test_a_missing_ledger_is_not_read_as_a_rejected_argument(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        (staged / "ledger.py").unlink()
        result = run_fix(staged, tmp_path / "findings.db", worktree, fence=fence())
        assert result.returncode not in (EXIT_INVALID, 1)


class TestABrokenGoldenPathRejectsTheFix:
    def test_a_refused_shell_row_is_thirty_and_names_the_row(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """The "tool became unusable" rejection, with the row a reviewer must act on."""
        db = tmp_path / "findings.db"
        refused = "gh pr view 1 --json state REFUSE-ME"
        path_id = a_golden_path(staged, command=refused)
        a_golden_path(staged, command="git status --porcelain")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, fence=fence("REFUSE-ME"))
        assert result.returncode == EXIT_BROKEN, result.stderr
        body = payload(result)
        assert body["verdict"] == "broken"
        assert [row["entry"] for row in body["broken"]] == [path_id]
        assert refused in body["broken"][0]["command_or_flow"]
        assert "stub refusal" in body["broken"][0]["why"]
        # The reason the human approved the row travels with the rejection: it is
        # what tells the reviewer whether to change the fix or retire the row.
        assert body["broken"][0]["reason"]

    @pytest.mark.parametrize(
        "tier, tag",
        [
            ("is_sensitive_bash_command", "sensitive-bash"),
            ("audit_bash_exfiltration", "exfil"),
            ("is_denied", "deny-rules"),
        ],
    )
    def test_a_refusal_from_any_tier_is_thirty_and_names_the_tier(
        self, staged: Path, tmp_path: Path, worktree: Path, tier: str, tag: str
    ) -> None:
        """The whole composite the tool gate applies, not the rule catalog alone.

        Measuring `is_denied` by itself goes green on a fix that tightened the path
        fence, the sensitive-command tier or the exfiltration auditor -- the specific
        way this gate could ship a meaningless pass. The tier is named in the reason
        because each one needs a different fix.
        """
        db = tmp_path / "findings.db"
        a_golden_path(staged, command="gh pr view 1 REFUSE-ME")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, fence=fence("REFUSE-ME", tier=tier))
        assert result.returncode == EXIT_BROKEN, result.stderr
        assert f"[{tag}]" in payload(result)["broken"][0]["why"]

    def test_the_tier_order_matches_deny_diff(self, mod) -> None:
        """Two gates, one claim. They agree only while they measure the same list."""
        source = (REPO_ROOT / "scripts" / "deny_diff.py").read_text(encoding="utf-8")
        start = source.index("_TIERS: tuple[tuple[str, str], ...] = (")
        end = source.index("\n)\n", start)
        declared = re.findall(r'\("([a-z-]+)",\s*"([a-z_]+)"\)', source[start:end])
        assert declared, "deny_diff's tier table was not found"
        assert list(mod.TIERS) == [tuple(pair) for pair in declared]


class TestHoldsIsUnreachableWhileAnythingIsUnverifiable:
    def test_a_tree_missing_one_tier_is_unverifiable(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """Two checks out of three is coverage lost, not two permits."""
        db = tmp_path / "findings.db"
        a_golden_path(staged, command="gh pr view 1 --json state")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, fence=fence(omit="audit_bash_exfiltration"))
        assert result.returncode == EXIT_UNVERIFIABLE, result.stderr
        assert "has no audit_bash_exfiltration" in payload(result)["unverifiable"][0]["why"]

    def test_an_absent_corpus_is_unverifiable_not_a_pass(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """A missing export checked nothing, and nothing is 20.

        Zero rows would fold to `holds` by construction -- the vacuous green a broken
        installation would report on every fix. The other case, corpus present but
        none for this host, is a real pass and is pinned by the platform tests.
        """
        db = tmp_path / "findings.db"
        assert not corpus_path(staged).exists()
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree)
        assert result.returncode == EXIT_UNVERIFIABLE, result.stderr
        body = payload(result)
        assert body["corpus_rows"] == 0
        assert body["golden_paths_checked"] == 0
        assert "not readable" in body["corpus_problems"][0]
        assert str(corpus_path(staged)) in body["corpus_problems"][0]

    @pytest.mark.parametrize(
        "content, symptom",
        [
            pytest.param("{not json", "does not load", id="not-json"),
            pytest.param('{"golden_paths": 3}', "'golden_paths' list", id="wrong-shape"),
            pytest.param('[{"kind": "shell"}]', "'golden_paths' list", id="bare-list"),
            pytest.param('{"golden_paths": [{"kind": "shell"}]}', "entry 0", id="malformed-row"),
            pytest.param('{"golden_paths": []}', "holds no golden path", id="empty"),
        ],
    )
    def test_a_corpus_that_does_not_load_is_unverifiable(
        self,
        staged: Path,
        tmp_path: Path,
        worktree: Path,
        content: str,
        symptom: str,
    ) -> None:
        """Validated by `ledger.py`'s own loader, so a row the import would refuse is
        a row the gate refuses to count -- named by its position, not skipped. An
        export with no rows is the same verdict: zero rows checked is the vacuous
        pass this ladder exists to make unreachable, and `scripts/deny_diff.py`
        refuses an empty corpus for the same reason."""
        db = tmp_path / "findings.db"
        corpus_path(staged).write_text(content, encoding="utf-8")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree)
        assert result.returncode == EXIT_UNVERIFIABLE, result.stderr
        body = payload(result)
        assert body["corpus_rows"] == 0
        assert body["golden_paths_checked"] == 0
        assert symptom in body["corpus_problems"][0]

    def test_an_unreadable_deny_fence_makes_every_shell_row_unverifiable(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """A fence that cannot be read is not a fence that agreed.

        This is the case that silently ships a broken tool: the probe fails, the
        shell rows go unchecked, and a script that treated "no refusal found" as
        "permitted" would report 0 having classified nothing.
        """
        db = tmp_path / "findings.db"
        a_golden_path(staged, command="gh pr view 1 --json state")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, fence=fence(available=False))
        assert result.returncode == EXIT_UNVERIFIABLE, result.stderr
        body = payload(result)
        assert body["verdict"] == "unverifiable"
        assert body["broken"] == []
        assert "not importable" in body["unverifiable"][0]["why"]


class TestTheFenceClassifiedAgainstIsTheWorktrees:
    """A leading ``PYTHONPATH`` entry is a preference, not a guarantee.

    A checkout that does not carry ``kiro_crew/security`` imports the INSTALLED
    package instead, and every golden path is then classified against rules that
    are not under review -- a pass that says nothing about the fix. The probe
    therefore proves where the fence came from, and a borrowed one is unavailable.
    """

    def test_a_worktree_without_the_package_does_not_borrow_the_installed_fence(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """No fake fence is staged, so the only ``kiro_crew.security`` the probe can
        find is the one installed in this interpreter -- which must not count."""
        db = tmp_path / "findings.db"
        a_golden_path(staged, command="gh pr view 1 --json state")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree)
        assert result.returncode == EXIT_UNVERIFIABLE, result.stderr
        body = payload(result)
        assert body["broken"] == []
        why = body["unverifiable"][0]["why"]
        # The interpreter running this suite has the package importable (the suite
        # itself imports it), so the borrow really happens and is really refused.
        assert "outside the worktree's src" in why, why
        assert str(worktree / "src") in why

    def test_a_fence_under_a_symlinked_src_is_the_worktrees(self, mod, tmp_path: Path) -> None:
        """A worktree whose ``src`` is a symlink to the source tree (the way an
        end-to-end smoke stages one) still owns the fence it points at."""
        real = tmp_path / "real-src"
        (real / "kiro_crew").mkdir(parents=True)
        module = real / "kiro_crew" / "security.py"
        module.write_text("", encoding="utf-8")
        link = tmp_path / "wt" / "src"
        link.parent.mkdir()
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available here")
        assert mod.fence_provenance_problem(str(module), str(link)) is None

    @pytest.mark.parametrize(
        "module_file, root, symptom",
        [
            pytest.param(
                "site-packages/kiro_crew/security/__init__.py", "wt/src", "outside", id="installed"
            ),
            pytest.param(None, "wt/src", "no source file", id="namespace-package"),
            pytest.param("wt/src/kiro_crew/security.py", "", "not told", id="no-root"),
            pytest.param(
                "wt/src-other/kiro_crew/security.py", "wt/src", "outside", id="sibling-prefix"
            ),
        ],
    )
    def test_a_borrowed_fence_is_named(
        self, mod, tmp_path: Path, module_file: str | None, root: str, symptom: str
    ) -> None:
        file_path = str(tmp_path / module_file) if module_file else None
        root_path = str(tmp_path / root) if root else ""
        problem = mod.fence_provenance_problem(file_path, root_path)
        assert problem is not None and symptom in problem

    def test_the_probe_is_told_the_fence_root(self) -> None:
        source = VERIFY_FIX.read_text(encoding="utf-8")
        assert '"fence_root": str(fence_root)' in source
        assert "fence_provenance_problem(getattr(security, " in source


class TestTheProbePayloadIsParsedDefensively:
    """The pure parser, tested directly.

    The shipped probe is the ONLY producer of this payload and always writes a
    well-formed object, so no malformed shape is reachable from the subprocess path
    -- and a second producer for tests would be exactly the argv seam this file no
    longer has. The parser still validates what it did not construct in-process,
    because an unreadable answer has a verdict (20) and a raise has none (exit 1,
    outside the contract).
    """

    @pytest.mark.parametrize(
        "body, fragment",
        [
            pytest.param("", "no JSON verdict", id="empty"),
            pytest.param("not json", "no JSON verdict", id="not-json"),
            pytest.param("[1, 2, 3]", "not an object", id="json-list"),
            pytest.param("42", "not an object", id="json-number"),
            pytest.param('"available"', "not an object", id="json-string"),
            pytest.param('{"available": false, "error": "x"}', "x", id="unavailable"),
            pytest.param('{"available": false}', "not importable", id="unavailable-no-error"),
            pytest.param(
                '{"available": true, "results": {"a": null}}',
                "no result list",
                id="results-not-a-list",
            ),
            pytest.param(
                '{"available": true, "results": [["cmd", null]]}',
                "malformed result entry",
                id="entry-not-object",
            ),
            pytest.param(
                '{"available": true, "results": [{"command": 7}]}',
                "malformed result entry",
                id="command-not-text",
            ),
            pytest.param(
                '{"available": true, "results": [{"command": "git status", "reason": 5}]}',
                "non-text refusal reason",
                id="reason-not-text",
            ),
        ],
    )
    def test_a_malformed_payload_is_unavailable_and_names_the_guard(
        self, mod, body: str, fragment: str
    ) -> None:
        available, results, note = mod.parse_probe_output(body, ["git status"])
        assert available is False
        assert results == {}
        # The SPECIFIC guard, not just the verdict: the skipped-commands check is a
        # second net that would otherwise pass for a missing shape check.
        assert fragment in note

    def test_a_partial_answer_is_not_a_verdict_about_the_rest(self, mod) -> None:
        body = '{"available": true, "results": [{"command": "a", "reason": null}]}'
        available, results, note = mod.parse_probe_output(body, ["a", "b"])
        assert available is False
        assert "skipped 1 command" in note

    def test_a_wellformed_answer_carries_each_reason_through(self, mod) -> None:
        body = (
            '{"available": true, "results": ['
            '{"command": "a", "reason": null}, {"command": "b", "reason": "rule X"}]}'
        )
        available, results, note = mod.parse_probe_output(body, ["a", "b"])
        assert available is True
        assert results == {"a": None, "b": "rule X"}
        assert note == ""

    def test_only_the_last_line_is_the_verdict(self, mod) -> None:
        """A fence that prints at import time must not hide the verdict."""
        body = (
            'warning: something\n{"available": true, "results": [{"command": "a", "reason": null}]}'
        )
        available, results, _ = mod.parse_probe_output(body, ["a"])
        assert available is True
        assert results == {"a": None}


class TestTheLedgerPathIsResolvedOnceAndShared:
    """Both processes must read the SAME ledger.

    The child runs with ``HOME`` pointed at the worktree so a ``~``-relative path
    lands in the throwaway checkout -- and the ledger's default path is
    ``HOME``-relative, so a child left to its own default reads a different
    database and reports "no such finding" for a finding that is right there.
    """

    def test_the_child_is_given_the_parents_resolved_default(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        # A verifier stub that records the argv it was handed. The path is baked
        # into its source because the script under test owns that argv -- there is
        # no flag through which a test could pass the recorder a destination.
        record = tmp_path / "verifier-argv.json"
        (staged / "verify_finding.py").write_text(
            "import json\nimport sys\n\n"
            f"open({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            "sys.exit(10)\n",
            encoding="utf-8",
        )
        home = tmp_path / "crew-home"
        a_golden_path(staged, command="git status --porcelain")
        result = run_fix(staged, None, worktree, fence=fence(), env={"KIROCREW_HOME": str(home)})
        assert result.returncode == EXIT_HOLDS, result.stderr
        argv = json.loads(record.read_text(encoding="utf-8"))
        assert "--db" in argv
        given = Path(argv[argv.index("--db") + 1])
        assert given.is_absolute()
        assert given == (home / "security-conductor" / "findings.db").resolve()


class TestNothingFromTheCorpusIsExecuted:
    """The security property, and the reason the flow lane checks nothing.

    A corpus row is text in a JSON file, and the same text once imported sits in a
    table whose CLI ``ledger.py`` says outright is not an authentication boundary:
    ``--approved-by`` is an unverified caller assertion. So a row is untrusted text
    written by whoever could edit the file or reach the database, and running one
    as argv would turn a file edit into command execution with the operator's
    access -- a privilege escalation no containment fixes, because the escalation
    is in treating the row as permission. So a non-shell row is reported for a
    human and never run.

    Each test plants a witness file the row would create if it ran.
    """

    def witness(self, tmp_path: Path, name: str) -> tuple[Path, str]:
        marker = tmp_path / f"{name}-fired"
        script = tmp_path / f"{name}_body.py"
        script.write_text(f"open({str(marker)!r}, 'w').write('fired')\n", encoding="utf-8")
        return marker, f"{sys.executable} {script}"

    def test_a_flow_row_is_reported_and_never_run(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        db = tmp_path / "findings.db"
        marker, command = self.witness(tmp_path, "flow")
        path_id = a_golden_path(staged, kind="flow", command=command)
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, fence=fence())
        assert result.returncode == EXIT_HOLDS, result.stderr
        assert not marker.exists(), "a corpus row was executed"
        body = payload(result)
        assert [row["entry"] for row in body["needs_human"]] == [path_id]
        assert "never run from the corpus" in body["needs_human"][0]["why"]

    def test_a_cron_row_is_reported_and_never_run(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """Firing a schedule also has effects outside the worktree no deadline bounds."""
        db = tmp_path / "findings.db"
        marker, command = self.witness(tmp_path, "cron")
        a_golden_path(staged, kind="cron", command=f"17 3 * * * :: {command}")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, fence=fence())
        assert result.returncode == EXIT_HOLDS, result.stderr
        assert not marker.exists(), "a corpus row was executed"
        assert payload(result)["needs_human"][0]["kind"] == "cron"

    def test_a_shell_row_is_classified_and_never_run(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """The CHECKED kind is not an exception: the fence reads the text, nothing runs it."""
        db = tmp_path / "findings.db"
        marker, command = self.witness(tmp_path, "shell")
        a_golden_path(staged, command=command)
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, fence=fence())
        assert result.returncode == EXIT_HOLDS, result.stderr
        assert not marker.exists(), "a corpus row was executed"

    def test_a_human_row_never_moves_the_exit_code(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """A row this script never claimed to check cannot make its verdict worse.

        The distinction is deliberate: ``unverifiable`` means a check THIS SCRIPT
        OWNS could not be settled, and an MCP tool was never one of them. Folding
        the human corpus into 20 would make the gate permanently unable to pass
        while saying nothing new.
        """
        db = tmp_path / "findings.db"
        a_golden_path(staged, kind="flow", command="monitor_start")
        a_golden_path(staged, kind="cron", command="every:300 :: rotation-check")
        a_golden_path(staged, command="git status --porcelain")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, fence=fence())
        assert result.returncode == EXIT_HOLDS, result.stderr
        body = payload(result)
        assert len(body["needs_human"]) == 2
        # Only the shell row was CHECKED, and the count says so rather than
        # reporting the whole table as verified.
        assert body["golden_paths_checked"] == 1


class TestTheSpawnPathCarriesNoCorpusText:
    """The absence a witness file cannot pin.

    ``TestNothingFromTheCorpusIsExecuted`` proves no row becomes a command line
    today. This proves the lane is not there to be reintroduced by an edit that looks
    reasonable: every child this script spawns is a checked-in sibling script, this
    script itself, or ``pytest``, and the function that walks golden paths assembles
    no argv of its own.

    A ``test`` row does reach a spawn, and that is the one kind that may: it names a
    pytest selector, which is handed over as a POSITIONAL argument after ``--`` rather
    than becoming argv. So the census below is per site and by name -- a bare count
    would have been bumped from three to six by this change and stopped saying which
    sites are sanctioned, which is the whole tripwire.
    """

    def source(self) -> str:
        return VERIFY_FIX.read_text(encoding="utf-8")

    def function_body(self, name: str) -> str:
        source = self.source()
        start = source.index(f"def {name}(")
        rest = source[start:]
        end = rest.index("\ndef ", 1)
        return rest[:end]

    def test_the_golden_path_walk_assembles_no_argv(self) -> None:
        """The walk itself spawns nothing; a row's own text never reaches a call."""
        body = self.function_body("check_golden_paths")
        assert "run_child(" not in body, "the golden-path walk reached a subprocess spawn"
        assert "subprocess" not in body

    def test_every_spawn_site_is_one_of_the_five_sanctioned_ones(self) -> None:
        """A sixth call site is a new trust decision and should not pass unnoticed.

        Each one is named, so adding a site fails this test rather than silently
        moving a number: the sibling verifier, this script's own classifier probe, the
        contract sibling, the pytest availability probe, and one behaviour row.
        """
        source = self.source()
        # The definition plus the five call sites.
        assert source.count("run_child(") == 6, source.count("run_child(")
        # 1. the sibling verifier, with room past the proof's own deadline
        assert "run_child(argv, worktree, timeout + REAP_SECONDS + 30)" in source
        # 2. the classifier probe: this file re-entered
        assert "probe_argv(worktree)," in self.function_body("classify_commands")
        # 3. the contract sibling, invoked by path like the verifier
        contract = self.function_body("run_contract_check")
        assert '"--worktree",' in contract and "str(script)," in contract
        # The base is the gate's, never the contract's: see CONTRACT_BASE.
        assert '"--base",' in contract and "CONTRACT_BASE," in contract
        assert "run_child(argv, worktree, timeout, capture=True)" in contract
        # 4 and 5. pytest, once to prove it runs at all and once per behaviour row,
        # both with the selector or --version as a positional argument.
        assert '[python, "-m", "pytest", *PYTEST_ARGS, "--version"]' in self.function_body(
            "pytest_available"
        )
        assert '[python, "-m", "pytest", *PYTEST_ARGS, "--", selector]' in self.function_body(
            "run_test_row"
        )

    def test_no_behaviour_row_can_become_an_option_or_another_tree(self) -> None:
        """The three selector shapes that are refused rather than run."""
        body = self.function_body("selector_problem")
        assert 'startswith("-")' in body
        assert "os.path.isabs(path_part)" in body
        assert '".."' in body

    def test_the_probe_argv_is_this_script_and_nothing_else(self) -> None:
        """The probe re-enters this file, and no flag can substitute another program.

        An argv assembled from a row would be the same escalation wearing the probe's
        name; an argv assembled from a FLAG is the same escalation wearing a testing
        seam's. Neither exists, and the parser has no option that names a program.
        """
        source = self.source()
        assert "[classifier_python(worktree), os.path.abspath(__file__), CLASSIFY_FLAG]" in source
        assert "classifier-cmd" not in source
        assert "override" not in self.function_body("classify_commands")

    def test_the_parser_declares_no_program_flag(self) -> None:
        """Pinned as an exact set: what matters is the flags that are ABSENT.

        No flag names a classifier program, another corpus, another platform, or the
        fix contract -- each would let the caller decide what the gate checks. The one
        addition since is ``--skip-test-rows``, which cannot shrink the corpus because
        the rows it leaves unrun are reported unverifiable and 0 stays unreachable.
        """
        body = self.function_body("_build_parser")
        flags = re.findall(r'add_argument\(\s*"(--[a-z-]+)"', body)
        assert set(flags) == {
            "--db",
            "--finding-id",
            "--worktree",
            "--timeout",
            "--contract",
        }


class TestThePlatformFilterIsBothWays:
    """The lock-in guard, exercised on whichever host runs the suite.

    The platform is derived from the host and from nothing else, so there is no
    flag to name the other host in a test; instead each half is stated relative to
    ``HOST`` and the suite runs on both CI hosts. A Windows-only shape reported
    broken on Linux would reject a fix for a host it was never checked on -- and
    the reverse is the same property, not symmetry for its own sake.
    """

    def test_the_other_hosts_row_is_not_checked_here(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        db = tmp_path / "findings.db"
        a_golden_path(staged, command="python -m pytest REFUSE-ME", platform=OTHER_HOST)
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, fence=fence("REFUSE-ME"))
        assert result.returncode == EXIT_HOLDS, result.stderr
        body = payload(result)
        assert body["platform"] == HOST
        assert body["golden_paths_checked"] == 0
        # Corpus present, none for this host: distinct from an empty corpus.
        assert body["corpus_rows"] == 1 and body["corpus_problems"] == []

    def test_this_hosts_row_is_checked_here(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        db = tmp_path / "findings.db"
        a_golden_path(staged, command="python -m pytest REFUSE-ME", platform=HOST)
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, fence=fence("REFUSE-ME"))
        assert result.returncode == EXIT_BROKEN, result.stderr
        assert payload(result)["golden_paths_checked"] == 1

    def test_an_any_row_is_checked_on_every_host(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        db = tmp_path / "findings.db"
        a_golden_path(staged, command="gh pr view 1 REFUSE-ME", platform="any")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, fence=fence("REFUSE-ME"))
        assert result.returncode == EXIT_BROKEN, result.stderr
        assert payload(result)["platform"] == HOST

    def test_the_filter_keeps_this_host_and_any_for_both_hosts(self, mod) -> None:
        """The pure filter, asked about both hosts in one process, since the
        subprocess tests can only ask about the one they run on."""
        rows = [
            {"platform": "posix", "n": 1},
            {"platform": "windows", "n": 2},
            {"platform": "any", "n": 3},
        ]
        assert [r["n"] for r in mod.rows_for_host(rows, "posix")] == [1, 3]
        assert [r["n"] for r in mod.rows_for_host(rows, "windows")] == [2, 3]

    def test_the_host_platform_is_never_any(self, mod) -> None:
        """``any`` is a property of a ROW, not a host. Resolving to it would select
        the ``any`` rows and silently drop every platform-specific one."""
        assert mod.host_platform() == HOST
        assert mod.host_platform() != "any"


class TestTheGateReadsTheCommittedFileNotTheLedger:
    """RFC: "both gates read the file and nothing else, and a row that is not in the
    committed export does not gate."

    The table is mutable by anything that can reach the database, so a gate that
    read it could be steered by a row flip: retire the one row the fix broke, and
    the gate goes green with the fix unchanged. Pinned from both sides, and then a
    third: the copy read is the skill's own, not the worktree's, so the change under
    review cannot rewrite the gate that judges it. And no ARGUMENT can shrink the
    corpus either: there is no flag naming another file or the other host, because
    a caller who could name one could name a smaller one.
    """

    def test_a_row_only_in_the_ledger_does_not_gate(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path
    ) -> None:
        db = tmp_path / "findings.db"
        a_ledger_golden_path(ledger_mod, db, command="gh pr view 1 REFUSE-ME")
        a_golden_path(staged, command="git status --porcelain")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, fence=fence("REFUSE-ME"))
        assert result.returncode == EXIT_HOLDS, result.stderr
        body = payload(result)
        assert body["golden_paths_checked"] == 1
        assert body["corpus_rows"] == 1

    def test_retiring_a_row_in_the_ledger_does_not_shrink_the_gate(
        self, staged: Path, ledger_mod, tmp_path: Path, worktree: Path
    ) -> None:
        """The bypass the file exists to close: the row the fix broke is flipped to
        ``active=0`` in the ledger, and the committed corpus still rejects the fix."""
        db = tmp_path / "findings.db"
        refused = "gh pr view 1 REFUSE-ME"
        a_ledger_golden_path(ledger_mod, db, command=refused, active=False)
        entry = a_golden_path(staged, command=refused)
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, fence=fence("REFUSE-ME"))
        assert result.returncode == EXIT_BROKEN, result.stderr
        assert [row["entry"] for row in payload(result)["broken"]] == [entry]

    def test_the_corpus_read_is_the_skills_own_not_the_worktrees(
        self, staged: Path, tmp_path: Path, worktree: Path
    ) -> None:
        """A checkout that ships an emptier corpus is still judged by this one."""
        db = tmp_path / "findings.db"
        shipped = worktree / "src" / "kiro_crew" / "builtin_skills" / "security-conductor"
        shipped.mkdir(parents=True)
        (shipped / CORPUS_FILENAME).write_text('{"golden_paths": []}', encoding="utf-8")
        a_golden_path(staged, command="gh pr view 1 REFUSE-ME")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, fence=fence("REFUSE-ME"))
        assert result.returncode == EXIT_BROKEN, result.stderr
        assert payload(result)["corpus"] == str(corpus_path(staged))

    @pytest.mark.parametrize(
        "flag",
        [
            pytest.param(["--corpus", "elsewhere.json"], id="corpus"),
            pytest.param(["--platform", OTHER_HOST], id="platform"),
        ],
    )
    def test_no_flag_names_another_corpus_or_the_other_host(
        self, staged: Path, tmp_path: Path, worktree: Path, flag: list[str]
    ) -> None:
        """Either flag would let the fixer running the gate choose what it checks:
        a smaller file, or the host whose rows are not this one's."""
        db = tmp_path / "findings.db"
        a_golden_path(staged, command="gh pr view 1 REFUSE-ME")
        install_verifier(staged, VERIFIER_REJECTED)
        result = run_fix(staged, db, worktree, fence=fence("REFUSE-ME"), extra=flag)
        assert result.returncode == EXIT_INVALID, result.stderr
        assert "unrecognized arguments" in result.stderr

    def test_the_script_opens_no_ledger_connection(self) -> None:
        """Read from the source: the only ledger call is the default-path resolver
        the verifier child is handed, so a table cannot be consulted by accident."""
        source = VERIFY_FIX.read_text(encoding="utf-8")
        assert "ledger.connect(" not in source
        assert "active_golden_paths" not in source
        assert "golden_paths WHERE" not in source
        assert source.count("ledger.load_golden_path_corpus(") == 1


class TestInvalidInputIsTwoNotAVerdict:
    @pytest.mark.parametrize(
        "extra",
        [
            pytest.param(["--timeout", "0"], id="nonpositive-timeout"),
            pytest.param(["--timeout", "-5"], id="negative-timeout"),
            pytest.param(["--platform", "darwin"], id="no-platform-flag"),
        ],
    )
    def test_a_bad_argument_is_two(
        self, staged: Path, tmp_path: Path, worktree: Path, extra: list[str]
    ) -> None:
        install_verifier(staged, VERIFIER_REJECTED)
        result = subprocess.run(
            [
                sys.executable,
                str(staged / "verify_fix.py"),
                "--db",
                str(tmp_path / "findings.db"),
                "--finding-id",
                "1",
                "--worktree",
                str(worktree),
                *extra,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            cwd=str(tmp_path),
        )
        assert result.returncode == EXIT_INVALID, result.stdout

    def test_a_worktree_that_is_not_a_checkout_is_two(self, staged: Path, tmp_path: Path) -> None:
        """A bare directory is not a disposable checkout, and that bound is the
        only containment a flow runs under."""
        install_verifier(staged, VERIFIER_REJECTED)
        plain = tmp_path / "not-a-checkout"
        plain.mkdir()
        result = subprocess.run(
            [
                sys.executable,
                str(staged / "verify_fix.py"),
                "--finding-id",
                "1",
                "--worktree",
                str(plain),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            cwd=str(tmp_path),
        )
        assert result.returncode == EXIT_INVALID
        assert "not a git checkout" in result.stderr

    def test_a_worktree_that_is_not_a_directory_is_two(self, staged: Path, tmp_path: Path) -> None:
        install_verifier(staged, VERIFIER_REJECTED)
        result = subprocess.run(
            [
                sys.executable,
                str(staged / "verify_fix.py"),
                "--finding-id",
                "1",
                "--worktree",
                str(tmp_path / "nowhere"),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            cwd=str(tmp_path),
        )
        assert result.returncode == EXIT_INVALID
        assert "not a directory" in result.stderr


class TestTheVerdictLadderIsDeclaredOnce:
    def test_the_fold_returns_the_strongest_verdict(self, mod) -> None:
        assert mod.fold_verdict("holds") == "holds"
        assert mod.fold_verdict("holds", "unverifiable") == "unverifiable"
        assert mod.fold_verdict("holds", "unverifiable", "broken") == "broken"
        assert mod.fold_verdict("holds", "unverifiable", "broken", "reproduces") == "reproduces"

    def test_every_verdict_has_an_exit_code_and_they_are_distinct(self, mod) -> None:
        assert set(mod.VERDICT_PRECEDENCE) == set(mod.EXIT_CODES)
        codes = list(mod.EXIT_CODES.values())
        assert sorted(codes) == sorted(set(codes))
        # The one code that must never be reachable from a non-holding verdict.
        assert mod.EXIT_CODES["holds"] == 0
        assert 0 not in [code for name, code in mod.EXIT_CODES.items() if name != "holds"]


class TestTheShippedCorpusIsALiveGate:
    """The corpus is only worth something if the real fence agrees with it.

    Every other class here stubs the classifier so a refusal can be arranged. This
    one does the opposite and asks the REAL ``is_denied`` about every shipped
    ``shell`` row, which is the assertion the corpus exists to make: a change to
    the deny fence that eats one of these operations fails here, at PR time,
    instead of in a maintainer's terminal a week later.
    """

    @pytest.fixture(scope="class")
    def rows(self, request) -> list[dict]:
        ledger_mod = load_skill_script("security_conductor_ledger_for_corpus", LEDGER)
        return ledger_mod.load_golden_path_corpus(CORPUS.read_text(encoding="utf-8"))

    def test_the_seed_carries_a_real_corpus(self, rows: list[dict]) -> None:
        # A band, not a count: the point is "a real corpus, not a stub and not an
        # unreviewed dump". The ceiling moved when the denial differential adopted
        # this file as its own corpus and the rows that only its retired fixture
        # carried were folded in here.
        assert 25 <= len(rows) <= 60, len(rows)
        assert all(row["reason"] for row in rows)
        kinds = {row["kind"] for row in rows}
        # Four kinds ship, and each is one the verifier knows what to do with: a
        # "shell" row it classifies against the real fence, "flow" and "cron" rows
        # it records for a human to exercise, and a "test" row it runs as pytest.
        assert kinds == {"shell", "flow", "cron", "test"}
        # Both halves of the lock-in guard are present, or the corpus asserts
        # nothing about the second failure mode it exists for.
        platforms = {row["platform"] for row in rows}
        assert {"posix", "windows"} <= platforms

    def test_every_shipped_shell_row_is_permitted_by_the_real_deny_composite(
        self, mod, rows: list[dict]
    ) -> None:
        """Every tier the gate applies to shell text, in its order -- the same composite the probe
        applies and `scripts/deny_diff.py` measures."""
        import kiro_crew.security as security

        broken: dict[str, str] = {}
        for row in rows:
            if row["kind"] != "shell":
                continue
            command = row["command_or_flow"]
            for name, attribute in mod.TIERS:
                outcome = getattr(security, attribute)(command)
                if outcome:
                    broken[command] = f"[{name}] {outcome}"
                    break
        assert not broken, json.dumps(broken, indent=2, sort_keys=True)

    def test_every_shipped_test_row_resolves_in_this_repository(self, rows: list[dict]) -> None:
        """A shipped ``test`` row names a node that EXISTS here, asserted at review time.

        The other rows in this class are checked for shape, and a selector's shape says
        nothing about whether the node it names is still there. A rename or a deleted
        test leaves the row pointing at nothing, ``verify_fix.py`` reads that as
        ``unverifiable`` on every fix from then on, and the corpus carries a row that
        gates nothing while looking like it does.

        Resolution is read out of the module's own AST rather than by running pytest.
        A nested ``--collect-only`` would inherit this host's temp directory, and on a
        host whose temp root sits inside the live data home the child's own guard
        refuses the run -- a test that passes in CI and fails on a maintainer's box.
        What this asserts is therefore narrower than collection, and it is the half
        that rots: the file is present and every ``::`` name is defined in it.
        """
        selectors = [row["command_or_flow"] for row in rows if row["kind"] == "test"]
        assert selectors, "the corpus carries no test row for this check to resolve"
        unresolved: dict[str, str] = {}
        for selector in selectors:
            path_part, *names = selector.split("::")
            module = REPO_ROOT / path_part
            if not module.is_file():
                unresolved[selector] = f"{path_part} is not a file in this repository"
                continue
            try:
                scope: list[ast.stmt] = ast.parse(
                    module.read_text(encoding="utf-8"), filename=str(module)
                ).body
            except SyntaxError as exc:  # pragma: no cover - a parse error is its own bug
                unresolved[selector] = f"{path_part} does not parse: {exc}"
                continue
            for name in names:
                match = next(
                    (
                        node
                        for node in scope
                        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name == name
                    ),
                    None,
                )
                if match is None:
                    unresolved[selector] = f"{path_part} defines no {name}"
                    break
                scope = match.body
        assert not unresolved, json.dumps(unresolved, indent=2, sort_keys=True)

    def test_every_shipped_cron_row_is_a_wellformed_pair(self, rows: list[dict]) -> None:
        """Asserted HERE, at review time, rather than by the script at run time.

        A cron row's well-formedness is a property of the checked-in corpus, and a
        parse in the verifier could never be a verdict about a FIX anyway -- it
        would read the ledger with the verifier's own regexes and answer the same
        constant no matter what the worktree contained.
        """
        problems = {}
        for row in rows:
            if row["kind"] != "cron":
                continue
            text = row["command_or_flow"]
            schedule, separator, command = text.partition("::")
            if not separator:
                problems[text] = "no '::' separator"
                continue
            schedule, command = schedule.strip(), command.strip()
            if not schedule or not command:
                problems[text] = "empty half"
                continue
            if not re.fullmatch(r"every:[1-9][0-9]*", schedule, re.IGNORECASE):
                fields = schedule.split()
                if len(fields) != 5:
                    problems[text] = f"{len(fields)} schedule fields"
                    continue
                if any(not re.fullmatch(r"[0-9*,/\-A-Za-z]+", f) for f in fields):
                    problems[text] = "unparseable schedule field"
                    continue
            try:
                if not shlex.split(command):
                    problems[text] = "command splits to nothing"
            except ValueError as exc:
                problems[text] = f"unparseable command: {exc}"
        assert not problems, problems

    def test_no_shipped_row_carries_a_scheme_prefix(self, rows: list[dict]) -> None:
        """A ``<scheme>::`` marker existed to tell an executable flow from a declared
        one. Nothing from the corpus is executed, so a prefix left behind would
        describe machinery that is not there."""
        flows = [row["command_or_flow"] for row in rows if row["kind"] == "flow"]
        assert flows
        assert not [text for text in flows if re.match(r"^[a-z][a-z0-9_-]*::", text)]

    def test_the_seed_imports_idempotently(self, ledger_mod, tmp_path: Path) -> None:
        db = tmp_path / "seeded.db"
        conn = ledger_mod.connect(db)
        try:
            ledger_mod.init_schema(conn)
            rows = ledger_mod.load_golden_path_corpus(CORPUS.read_text(encoding="utf-8"))
            first = ledger_mod.import_golden_paths(conn, rows, approved_by="tester")
            second = ledger_mod.import_golden_paths(conn, rows, approved_by="tester")
        finally:
            conn.close()
        assert first["imported"] == len(rows)
        assert second == {"imported": 0, "skipped": len(rows), "total": len(rows)}


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
