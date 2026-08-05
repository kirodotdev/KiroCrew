"""Path handling in the pod-e2e harness shell script.

Two bugs made this suite unrunnable or unresolvable on real hosts, and neither
was covered:

* The artifact-dir containment guard resolved the CANDIDATE path but compared it
  against a pattern built from the UNRESOLVED ``$HOME``. On a host where ``~`` is
  a symlink (the standard Amazon dev-desktop layout, ``/home/<u>`` ->
  ``/local/home/<u>``) the two sides disagreed and every run aborted with exit 65
  before executing a single phase. It only reproduced where ``readlink -f``
  exists: on macOS, whose BSD ``readlink`` has no ``-f`` before Ventura, the
  command failed and both sides fell back to the unresolved path, hiding it.
* ``_resolve_checkout`` matched only the worktree DIRECTORY basename, including
  in the branch-matching awk branch, so a short pod name that ``kirocrew pod up``
  accepts (it resolves ``feat/<name>``) was unresolvable whenever the directory
  basename differed from the branch leaf.

These tests drive the real shell fragments out of the shipped script, so they
fail if either regresses.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "src/kiro_crew/apps/builtins/dev_fleet/skills/pod-e2e/scripts/pod-e2e.sh"
)


def _bash_works() -> bool:
    """A *working* bash, not merely a file named bash.

    Windows runners ship C:\\Windows\\System32\\bash.exe — the WSL launcher —
    ahead of Git Bash on PATH. `shutil.which` finds it, but with no WSL distro
    installed it prints "Windows Subsystem for Linux has no installed
    distributions" (in UTF-16) and runs nothing, so an existence check let these
    shell-fragment tests run against a stub and fail on its error banner.
    """
    if shutil.which("bash") is None:
        return False
    try:
        probe = subprocess.run(
            ["bash", "-c", "echo ok"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and probe.stdout.strip() == "ok"


pytestmark = pytest.mark.skipif(
    not _bash_works(), reason="harness fragments need a working bash (not the WSL stub)"
)


def _fragment(start: str, end: str) -> str:
    """Slice a fragment out of the shipped script (inclusive of *end*).

    The script contains UTF-8 punctuation; without an explicit encoding this
    raised UnicodeDecodeError at import time on Windows (cp1252 default),
    erroring the module before its bash skipif could even apply.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    i = src.index(start)
    j = src.index(end, i) + len(end)
    return src[i:j]


def _run(
    snippet: str,
    home: str,
    extra_path: str | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess:
    path = "/usr/bin:/bin:/usr/sbin:/sbin"
    if extra_path:
        path = f"{extra_path}:{path}"
    return subprocess.run(
        ["bash", "-c", snippet],
        env={"HOME": home, "PATH": path},
        capture_output=True,
        text=True,
        input=stdin,
    )


@pytest.fixture()
def gnu_readlink(tmp_path: Path) -> str:
    """A `readlink -f` that really resolves, so the bug's precondition holds.

    macOS ships a BSD readlink without -f; without this shim the original bug is
    invisible on a Mac and the test would vacuously pass there.
    """
    bindir = tmp_path / "shim"
    bindir.mkdir()
    shim = bindir / "readlink"
    shim.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os, sys
            args = [a for a in sys.argv[1:] if a not in ("-f", "--")]
            print(os.path.realpath(args[0]))
            """
        )
    )
    shim.chmod(0o755)
    return str(bindir)


@pytest.fixture()
def symlinked_home(tmp_path: Path) -> str:
    """`$HOME` that is a symlink to its physical location."""
    real = tmp_path / "physical"
    real.mkdir()
    link = tmp_path / "home"
    link.symlink_to(real)
    return str(link)


# --------------------------------------------------------------------------
# guard: symmetric resolution, no GNU readlink dependency
# --------------------------------------------------------------------------

HELPER = _fragment("_realpath_dir() {", "\n}")
# Anchor the guard on its own assignment: the helper now contains an internal
# case/esac for `..` normalisation, so anchoring the whole block on "esac" would
# truncate at the helper's.
GUARD = HELPER + "\n" + _fragment("E2E_ARTIFACT_BASE=", "esac")


def test_guard_accepts_a_normal_name_under_a_symlinked_home(symlinked_home, gnu_readlink):
    """The regression: this aborted with exit 65 on every symlinked-HOME host."""
    res = _run(f'NAME=smoke\n{GUARD}\necho "OK:$ARTIFACT_DIR"', symlinked_home, gnu_readlink)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "escapes .e2e-artifacts" not in res.stderr
    assert "OK:" in res.stdout


def test_guard_works_without_any_readlink_at_all(symlinked_home, tmp_path):
    """`readlink -f` is a GNU extension; the guard must not depend on it."""
    empty = tmp_path / "no-readlink"
    empty.mkdir()
    res = subprocess.run(
        ["bash", "-c", f'NAME=smoke\n{GUARD}\necho OK'],
        env={"HOME": symlinked_home, "PATH": f"{empty}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    # basename/dirname/cd/pwd are all we may rely on
    assert res.returncode == 0, res.stdout + res.stderr
    assert "OK" in res.stdout


def test_guard_still_rejects_an_escaping_name(symlinked_home, gnu_readlink):
    """The guard's actual purpose must survive the fix."""
    res = _run(f'NAME=../../escape\n{GUARD}\necho SHOULD_NOT_REACH', symlinked_home, gnu_readlink)
    assert res.returncode == 65, res.stdout + res.stderr
    assert "escapes .e2e-artifacts" in res.stderr
    assert "SHOULD_NOT_REACH" not in res.stdout


def test_realpath_dir_tolerates_a_missing_leaf(symlinked_home):
    """It must resolve before `mkdir -p`, i.e. on the first ever run."""
    helper = _fragment("_realpath_dir() {", "\n}")
    res = _run(f'{helper}\n_realpath_dir "$HOME/nope/not/created/yet"', symlinked_home)
    assert res.returncode == 0, res.stderr
    out = res.stdout.strip()
    assert out.endswith("/nope/not/created/yet")
    assert os.path.realpath(symlinked_home) in out


def test_realpath_dir_collapses_dotdot_in_a_missing_tail(symlinked_home):
    """`readlink -f` normalises `..`; the portable replacement must too.

    Without this, `<base>/../../x` keeps `<base>` as a literal prefix and slips
    through the containment guard below.
    """
    helper = _fragment("_realpath_dir() {", "\n}")
    res = _run(f'{helper}\n_realpath_dir "$HOME/a/b/../../c"', symlinked_home)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == f"{os.path.realpath(symlinked_home)}/c"


# --------------------------------------------------------------------------
# resolver: must mirror pod/runtime.py resolve_checkout() exactly
# --------------------------------------------------------------------------

RESOLVER = _fragment("_resolve_checkout() {", "\n}")

PORCELAIN = (
    "worktree /repo\nHEAD aaa\nbranch refs/heads/main\n\n"
    # directory basename deliberately differs from the branch leaf
    "worktree /repo-wt-podsmoke\nHEAD bbb\nbranch refs/heads/feat/podsmoke\n\n"
)

# `fix/foo` is listed BEFORE `feat/foo`. A leaf-matching resolver picks fix/foo;
# the CLI picks feat/foo, because wts.get("foo") misses (no basename or exact
# branch equals "foo") and wts.get("feat/foo") hits.
PORCELAIN_AMBIGUOUS = (
    "worktree /repo-wt-fix\nHEAD aaa\nbranch refs/heads/fix/foo\n\n"
    "worktree /repo-wt-feat\nHEAD bbb\nbranch refs/heads/feat/foo\n\n"
)


def _resolve(name: str, home: str, porcelain: str = PORCELAIN, tmp: Path | None = None) -> str:
    """Run the REAL _resolve_checkout with a fake `git` feeding *porcelain*."""
    assert tmp is not None
    bindir = tmp / "gitshim"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "git"
    fake.write_text("#!/bin/sh\ncat <<'PORC'\n" + porcelain + "PORC\n")
    fake.chmod(0o755)
    snippet = f'HERE=/repo\n{RESOLVER}\n_resolve_checkout {name!r}'
    return _run(snippet, home, extra_path=str(bindir)).stdout.strip()


def test_resolver_matches_the_branch_via_feat_prefix(tmp_path):
    """`podsmoke` resolves through branch feat/podsmoke, as the CLI does."""
    assert _resolve("podsmoke", str(tmp_path), tmp=tmp_path) == "/repo-wt-podsmoke"


def test_resolver_matches_an_exact_branch(tmp_path):
    assert _resolve("feat/podsmoke", str(tmp_path), tmp=tmp_path) == "/repo-wt-podsmoke"


def test_resolver_matches_a_plain_branch(tmp_path):
    assert _resolve("main", str(tmp_path), tmp=tmp_path) == "/repo"


def test_resolver_matches_a_directory_basename(tmp_path):
    assert _resolve("repo-wt-podsmoke", str(tmp_path), tmp=tmp_path) == "/repo-wt-podsmoke"


def test_resolver_prefers_feat_over_another_branch_with_the_same_leaf(tmp_path):
    """Regression: a leaf match would pick fix/foo and test the WRONG checkout.

    `kirocrew pod up foo` resolves feat/foo, so the harness must too — otherwise
    the suite reports a verdict for a branch nobody booted.
    """
    got = _resolve("foo", str(tmp_path), porcelain=PORCELAIN_AMBIGUOUS, tmp=tmp_path)
    assert got == "/repo-wt-feat", f"picked {got!r}, must mirror the CLI's feat/ preference"


def test_resolver_reports_nothing_for_an_unknown_name(tmp_path):
    assert _resolve("nosuchpod", str(tmp_path), tmp=tmp_path) == ""
