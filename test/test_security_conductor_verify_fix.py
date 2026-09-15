"""``verify_fix.py`` — the two-step gate, and the verdict it must never reach.

The properties pinned here are the ones a prompt cannot hold. A fix is verified
only when the proof stops reproducing AND every legitimate operation still works,
so the interesting assertions are all about the verdict LADDER: a proof that still
reproduces outranks everything, a broken golden path is a rejection, and
``holds`` is unreachable while any golden path went unchecked.

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

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "src" / "kiro_crew" / "builtin_skills" / "security-conductor"
SCRIPTS = SKILL_DIR / "scripts"
VERIFY_FIX = SCRIPTS / "verify_fix.py"
LEDGER = SCRIPTS / "ledger.py"
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


def is_sensitive_path(command, *args, **kwargs):
    return TIER == "is_sensitive_path" and _refuses(command)


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
    """A scripts directory holding ``verify_fix.py`` and ``ledger.py`` and nothing else.

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

    ``tier`` names which of the four checks refuses the marker; ``omit`` deletes one
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
) -> subprocess.CompletedProcess[str]:
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
        argv, capture_output=True, text=True, encoding="utf-8", timeout=300, env=child
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
            ("is_sensitive_path", "sensitive-path"),
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
        """Three checks out of four is coverage lost, not three permits."""
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

    ``TestNothingFromTheCorpusIsExecuted`` proves no row runs today. This proves the
    lane is not there to be reintroduced by an edit that looks reasonable: the only
    two children this script spawns are a checked-in sibling script and itself, and
    the function that walks golden paths spawns nothing at all.
    """

    def source(self) -> str:
        return VERIFY_FIX.read_text(encoding="utf-8")

    def function_body(self, name: str) -> str:
        source = self.source()
        start = source.index(f"def {name}(")
        rest = source[start:]
        end = rest.index("\ndef ", 1)
        return rest[:end]

    def test_the_golden_path_walk_spawns_nothing(self) -> None:
        body = self.function_body("check_golden_paths")
        assert "run_child(" not in body, "a golden-path row reached a subprocess spawn"
        assert "subprocess" not in body

    def test_only_two_call_sites_spawn_at_all(self) -> None:
        """One for the sibling verifier, one for this script's own probe. A third is
        a new trust decision and should not pass unnoticed."""
        source = self.source()
        calls = source.count("run_child(")
        # The definition, the verifier call, the probe call.
        assert calls == 3, calls
        assert "run_child(argv, worktree, timeout + REAP_SECONDS + 30)" in source

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
        body = self.function_body("_build_parser")
        flags = re.findall(r'add_argument\(\s*"(--[a-z-]+)"', body)
        assert set(flags) == {"--db", "--finding-id", "--worktree", "--timeout"}


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
        assert kinds == {"shell", "flow", "cron"}
        # Both halves of the lock-in guard are present, or the corpus asserts
        # nothing about the second failure mode it exists for.
        platforms = {row["platform"] for row in rows}
        assert {"posix", "windows"} <= platforms

    def test_every_shipped_shell_row_is_permitted_by_the_real_deny_composite(
        self, mod, rows: list[dict]
    ) -> None:
        """All four tiers, in the tool gate's order -- the same composite the probe
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
