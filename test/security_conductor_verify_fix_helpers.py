"""The staging, stubs and subprocess driver the ``verify_fix.py`` tests share.

``test_security_conductor_verify_fix.py`` and
``test_security_conductor_verify_fix_contract_and_test_kind.py`` drive the gate the way the
conductor does: :func:`run_fix` launches ``verify_fix.py`` as a child process from a
``staged`` scripts directory holding the script, its ledger and the contract sibling,
against a ``worktree`` that passes the checkout screen. A test states the verifier's exit
status with :func:`install_verifier`, writes the corpus beside the skill with
:func:`a_golden_path`, and names the fake deny fence the probe imports with :func:`fence`;
:func:`payload` reads the verdict line. The ``EXIT_*`` and ``VERIFIER_*`` constants are the
two exit-code contracts the gate folds. ``mod``, ``staged`` and ``worktree`` are fixtures
declared with ``name=`` and imported under their function names, so a test that requests
one by parameter does not shadow the import.
"""

from __future__ import annotations

import json
import os
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
CHECK_FIX_CONTRACT = SCRIPTS / "check_fix_contract.py"
CORPUS = SKILL_DIR / "golden-paths.json"
CORPUS_FILENAME = CORPUS.name


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


@pytest.fixture(name="mod")
def mod_fixture():
    return load_skill_script("security_conductor_verify_fix", VERIFY_FIX)


@pytest.fixture(name="staged")
def staged_fixture(tmp_path: Path) -> Path:
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


@pytest.fixture(name="worktree")
def worktree_fixture(tmp_path: Path) -> Path:
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
    for ``TestTheShippedCorpusIsALiveGate`` in
    ``test_security_conductor_verify_fix.py``.
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
