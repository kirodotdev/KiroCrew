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
    os.name == "nt" or not _bash_works(),
    reason="harness fragments require POSIX path and process semantics plus bash",
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
    shim.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import os, sys
            args = [a for a in sys.argv[1:] if a not in ("-f", "--")]
            print(os.path.realpath(args[0]))
            """))
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
        ["bash", "-c", f"NAME=smoke\n{GUARD}\necho OK"],
        env={"HOME": symlinked_home, "PATH": f"{empty}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    # basename/dirname/cd/pwd are all we may rely on
    assert res.returncode == 0, res.stdout + res.stderr
    assert "OK" in res.stdout


def test_guard_still_rejects_an_escaping_name(symlinked_home, gnu_readlink):
    """The guard's actual purpose must survive the fix."""
    res = _run(f"NAME=../../escape\n{GUARD}\necho SHOULD_NOT_REACH", symlinked_home, gnu_readlink)
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
# args: a documented flag must be accepted AND reach the driver
#
# SKILL.md tells the operator to "pass --no-suppress-first-run" when testing the
# onboarding flow itself, and pod-playwright.py accepts it — but the runner's
# arg loop hit its `-*)` catch-all and exited 64, so the documented spelling
# aborted the run before a single phase. Accepting it without appending it to
# PW_ARGS would be just as broken (a silent no-op), so both halves are driven
# out of the shipped script here.
# --------------------------------------------------------------------------

ARGS = _fragment('NAME="" ; KEEP=0', "\ndone")
PW_BUILD = _fragment('PW_ARGS=("$PW_RUNNER"', 'PW_CMD=("$PW_PY" -u "${PW_ARGS[@]}")')


def _driver_argv(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess:
    """Parse *argv* with the real loop, then build the real driver command.

    Coupling the two fragments is the point: a flag the parser accepts but the
    builder drops is the defect, and only the end-to-end argv shows it.
    """
    snippet = "\n".join(
        [
            "set -uo pipefail",
            ARGS,
            # Minimum context the construction block reads. MANIFEST is empty so
            # the --spec branch stays out of the way.
            "PW_RUNNER=/drv/pod-playwright.py ; PW_PY=/usr/bin/python3",
            "BASE_URL=http://127.0.0.1:7811 ; ARTIFACT_DIR=/art ; CHECKOUT=/wt",
            'MANIFEST=""',
            PW_BUILD,
            'printf "%s\\n" "${PW_CMD[@]}"',
        ]
    )
    return subprocess.run(
        ["bash", "-c", snippet, "pod-e2e.sh", *argv],
        cwd=tmp_path,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        encoding="utf-8",
    )


def test_no_suppress_first_run_is_accepted_and_forwarded(tmp_path):
    """The regression: the documented flag exited 64 instead of reaching the driver."""
    res = _driver_argv(tmp_path, "smoke", "--no-suppress-first-run")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "unknown flag" not in res.stderr, res.stderr
    argv = res.stdout.split()
    assert "--no-suppress-first-run" in argv, f"never forwarded to the driver: {argv}"
    # It must not be mistaken for the worktree NAME by the loop's `*)` arm.
    assert "NAME=--no-suppress-first-run" not in res.stdout


def test_first_run_suppression_stays_the_default(tmp_path):
    """Absent the flag, nothing is appended — suppression is the documented default."""
    res = _driver_argv(tmp_path, "smoke")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "--no-suppress-first-run" not in res.stdout, res.stdout


def test_unknown_flags_are_still_rejected(tmp_path):
    """The catch-all must survive: a typo may not be silently swallowed."""
    res = _driver_argv(tmp_path, "smoke", "--no-supress-first-run")
    assert res.returncode == 64, res.stdout + res.stderr
    assert "unknown flag" in res.stderr, res.stderr


def test_usage_text_lists_the_flag():
    """A flag the parser takes but the usage line hides is undiscoverable."""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    header = next(ln for ln in lines if ln.startswith("# pod-e2e.sh <worktree-name>"))
    usage = next(ln for ln in lines if ln.lstrip().startswith('[ -n "$NAME" ]'))
    for where, text in (("header", header), ("usage message", usage)):
        assert "--no-suppress-first-run" in text, f"{where} omits the flag: {text}"


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
    snippet = f"HERE=/repo\n{RESOLVER}\n_resolve_checkout {name!r}"
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


# ---------------------------------------------------------------------------
# Health phase: identity, not reachability.
#
# The phase used to bare-curl base_url/api/health and accept any 200/401/403. A
# pod's port is derived from its name across 199 slots and can be pinned by hand,
# so it is routinely held by another pod or by the live gateway, and every
# gateway answers that path identically -- so the poll could hand every later
# phase a pod this run never booted. It now reads the identity-gated verdict from
# `pod status --json`. These drive the real fragment out of the shipped script.
# ---------------------------------------------------------------------------
HEALTH_FRAGMENT_START = (
    "# ---------------------------------------------------------------- health --"
)
HEALTH_FRAGMENT_END = 'fail "health — pod never became healthy'


def _health_snippet(stub_bin: str, timeout: str = "1") -> str:
    """The shipped health fragment plus the minimum preamble it reads."""
    body = _fragment(HEALTH_FRAGMENT_START, HEALTH_FRAGMENT_END) + '"\n  fi\nfi\n'
    preamble = textwrap.dedent(f"""
        set -uo pipefail
        export POD_E2E_HEALTH_TIMEOUT='{timeout}'
        KIROCREW_CLI="{stub_bin}"
        NAME=demo
        BASE_URL=http://127.0.0.1:7811
        PORT=7811
        # Under the supplied HOME (pytest's tmp_path), never `mktemp -d`: `_run`
        # passes only HOME and PATH, so a bare mktemp would land in the host temp
        # root and stay there after the test.
        ARTIFACT_DIR="$HOME/artifacts"
        mkdir -p "$ARTIFACT_DIR"
        log() {{ :; }}
        fail() {{ echo "FAIL:$1"; }}
        """)
    return preamble + body + '\necho "HEALTHY=$HEALTHY FOREIGN=$FOREIGN"\n'


@pytest.fixture()
def stub_cli(tmp_path: Path):
    """A fake `kirocrew` whose `pod status --json` health value is injectable."""

    def _make(health: str) -> str:
        path = tmp_path / "kirocrew-stub"
        path.write_text(
            textwrap.dedent(f"""
                #!/usr/bin/env bash
                if [ "$1" = "pod" ] && [ "$2" = "status" ]; then
                  printf '{{"name":"demo","status":"up","port":7811,"health":%s}}\\n' '{health}'
                  exit 0
                fi
                exit 0
                """).lstrip(),
            encoding="utf-8",
        )
        path.chmod(0o755)
        return str(path)

    return _make


def test_health_accepts_the_pods_own_serving_codes(stub_cli, tmp_path):
    for code in ("200", "401", "403"):
        res = _run(_health_snippet(stub_cli(code)), str(tmp_path))
        assert res.returncode == 0, res.stdout + res.stderr
        assert "HEALTHY=1" in res.stdout, f"code {code}: {res.stdout}"
        assert "FAIL:" not in res.stdout, f"code {code} must not fail: {res.stdout}"


def test_health_refuses_a_foreign_port_holder_and_names_the_conflict(stub_cli, tmp_path):
    """-2 is a squatter, so the phase must NOT proceed, and must say why.

    Blaming the worktree build here is what sent an operator to read a journal
    that only says "address already in use"; the remedy is a free PORT=.
    """
    res = _run(_health_snippet(stub_cli("-2")), str(tmp_path))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "HEALTHY=0 FOREIGN=1" in res.stdout, res.stdout
    assert "FAIL:" in res.stdout, res.stdout
    assert "held by another process" in res.stdout, res.stdout
    assert "PORT=" in res.stdout, res.stdout


def test_health_reports_a_plain_timeout_when_nothing_answers(stub_cli, tmp_path):
    res = _run(_health_snippet(stub_cli("0")), str(tmp_path))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "HEALTHY=0 FOREIGN=0" in res.stdout, res.stdout
    assert "never became healthy" in res.stdout, res.stdout
    assert "held by another process" not in res.stdout, res.stdout


@pytest.mark.parametrize(
    "bad,why",
    [
        ("abc", "read as a variable NAME inside $(( )); set -u kills the run"),
        ("-5", "leading '-' is a non-digit, and a past deadline fakes a timeout"),
        ("999999999999999999999999", "no error, but the deadline lands centuries out"),
        ("6 0", "whitespace is not a digit"),
    ],
)
def test_health_survives_every_bad_timeout_override(stub_cli, tmp_path, bad, why):
    """A typo in one env var must cost a warning, never the run.

    Each of these was verified to abort or hang the unvalidated form directly --
    `abc` exits 127 with "unbound variable", and a 24-digit value does not error at
    all but sets a deadline centuries out, so the poll never gives up. For an
    unattended harness that silent hang is worse than the crashes. Validation
    therefore happens once, before the value reaches arithmetic.
    """
    res = _run(_health_snippet(stub_cli("200"), timeout=bad), str(tmp_path))
    assert res.returncode == 0, f"{why}: {res.stdout + res.stderr}"
    # Fell back to the default and still reached a real verdict.
    assert "HEALTHY=1" in res.stdout, f"{why}: {res.stdout}"
    assert "ignoring POD_E2E_HEALTH_TIMEOUT" in res.stderr, f"{why}: {res.stderr}"
    for boom in ("unbound variable", "value too great for base"):
        assert boom not in res.stderr, f"{why}: {res.stderr}"


@pytest.mark.parametrize("good", ["5", "08", "60", "999999"])
def test_health_accepts_a_valid_timeout_override(stub_cli, tmp_path, good):
    """The knob must still work -- validation that rejects everything is useless.

    `08` is deliberately in the accepted set, not the rejected one: a leading zero
    is a normal way to write a number, and it only broke because bash read it as
    octal ("value too great for base", exit 1). The explicit `10#` base makes it
    mean 8 seconds, so it is interpreted rather than refused.
    """
    res = _run(_health_snippet(stub_cli("200"), timeout=good), str(tmp_path))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "HEALTHY=1" in res.stdout, res.stdout
    assert "ignoring POD_E2E_HEALTH_TIMEOUT" not in res.stderr, res.stderr
    assert "value too great for base" not in res.stderr, res.stderr


@pytest.fixture()
def ticking_date(tmp_path: Path) -> str:
    """A `date` whose clock advances one whole second on every single call.

    The real flake needed a genuine second boundary to fall in the one-fork gap
    between the deadline assignment and the loop's own clock read -- a few
    milliseconds in 1000, so it surfaced as an unreproducible macOS CI failure
    rather than anything a test could pin. This shim makes that boundary land
    there every time: call one returns N, call two returns N+1. Nothing else on
    PATH is replaced, so the stub CLI and python3 still run normally.
    """
    bindir = tmp_path / "clockshim"
    bindir.mkdir()
    counter = tmp_path / "clock-counter"
    shim = bindir / "date"
    shim.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            n=$(cat '{counter}' 2>/dev/null || echo 1000)
            echo $((n + 1)) > '{counter}'
            echo "$n"
            """))
    shim.chmod(0o755)
    return str(bindir)


def test_health_probes_at_least_once_when_the_clock_ticks_past_the_deadline(
    stub_cli, tmp_path, ticking_date
):
    """The poll must be a do-while: probe, THEN judge the deadline.

    `date +%s` is whole-second, so the pre-test form read a clock that could have
    already ticked past a deadline computed one fork earlier and skipped its body
    entirely. The body is the only place HEALTHY is set, so a healthy pod was
    reported as "never became healthy" -- a false red whose message points the
    operator at a boot log that shows a perfectly healthy boot.
    """
    res = _run(_health_snippet(stub_cli("200")), str(tmp_path), extra_path=ticking_date)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "HEALTHY=1" in res.stdout, f"zero probes performed: {res.stdout}"
    assert "FAIL:" not in res.stdout, res.stdout


def test_health_still_gives_up_when_the_clock_runs_away(stub_cli, tmp_path, ticking_date):
    """Probing first must not cost the deadline -- an unhealthy pod still ends.

    Guards the other side of the same change: a do-while whose exit test was
    dropped would hang an unattended harness forever on a pod that never answers.
    With this clock every iteration burns a second, so the 1s deadline is past
    immediately after the first probe.
    """
    res = _run(_health_snippet(stub_cli("0")), str(tmp_path), extra_path=ticking_date)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "HEALTHY=0 FOREIGN=0" in res.stdout, res.stdout
    assert "never became healthy" in res.stdout, res.stdout
