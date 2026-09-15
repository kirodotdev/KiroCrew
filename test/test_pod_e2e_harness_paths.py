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

import ast
import json
import os
import re
import shlex
import shutil
import string
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "src/kiro_crew/apps/builtins/dev_fleet/skills/pod-e2e/scripts/pod-e2e.sh"
)
POD_CLI = Path(__file__).resolve().parent.parent / "src/kiro_crew/pod/cli.py"
POD_RUNTIME = Path(__file__).resolve().parent.parent / "src/kiro_crew/pod/runtime.py"
POD_CONFIG = Path(__file__).resolve().parent.parent / "src/kiro_crew/pod/config.py"
TOKEN_AUTH = Path(__file__).resolve().parent.parent / "src/kiro_crew/dashboard/token_auth.py"
TOKEN_HANDLER = Path(__file__).resolve().parent.parent / "src/kiro_crew/dashboard/handlers/core.py"
WORKTREE_OPS = (
    Path(__file__).resolve().parent.parent / "src/kiro_crew/apps/builtins/dev_fleet/worktree_ops.py"
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


@pytest.mark.parametrize("flag", ["--no-suppress-first-run", "--handle-json"])
def test_usage_text_lists_the_flag(flag):
    """A flag the parser takes but the usage line hides is undiscoverable."""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    header = next(ln for ln in lines if ln.startswith("# pod-e2e.sh <worktree-name>"))
    usage = next(ln for ln in lines if ln.lstrip().startswith('[ -n "$NAME" ]'))
    for where, text in (("header", header), ("usage message", usage)):
        assert flag in text, f"{where} omits the flag: {text}"


# --------------------------------------------------------------------------
# no test-suite phase
# --------------------------------------------------------------------------


def test_the_harness_never_runs_the_worktrees_test_suite():
    """No executable line in the harness may invoke the checkout's test suite.

    A `python -m pytest -q` from the checkout root is ~62k tests that need no
    pod, that CI runs on the merge ref anyway, and whose fan-out costs far more
    than the browser check this harness exists for. Only the comment explaining
    the absence may name pytest, so an executable line that invokes it fails
    here.
    """
    offenders = [
        ln
        for ln in SCRIPT.read_text(encoding="utf-8").splitlines()
        if "pytest" in ln and not ln.lstrip().startswith("#")
    ]
    assert not offenders, f"the test-suite phase came back: {offenders}"


def test_fe_only_is_still_accepted_after_the_phase_was_removed(tmp_path):
    """Older invocations pass ``--fe-only``; with no suite phase left to skip it
    is a no-op, and must not become an exit-64 unknown flag."""
    res = _driver_argv(tmp_path, "smoke", "--fe-only")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "unknown flag" not in res.stderr, res.stderr
    # And it may not be mistaken for the worktree NAME by the loop's `*)` arm.
    assert "NAME=--fe-only" not in res.stdout


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
# Bare-curling base_url/api/health and accepting any 200/401/403 is unsafe. A
# pod's port is derived from its name across 199 slots and can be pinned by hand,
# so it is routinely held by another pod or by the live gateway, and every
# gateway answers that path identically -- so the poll could hand every later
# phase a pod this run never booted. It now reads the identity-gated verdict from
# `pod status --json`. These drive the real fragment out of the shipped script.
# ---------------------------------------------------------------------------
HEALTH_FRAGMENT_START = (
    "# ---------------------------------------------------------------- health --"
)
# Anchored on the LAST line of the phase, including its closing `fi`, so the
# fragment is self-contained. Ending mid-string and re-appending the quote plus a
# guessed number of `fi`s hid a real truncation once: bash parses `-c` input
# incrementally, so an unterminated `if` at the end is not a syntax error, it is
# silence -- the tests died on an unrelated unbound variable instead.
HEALTH_FRAGMENT_END = 'not polled here"\nfi'


def _health_snippet(stub_bin: str, timeout: str = "1", handle: str | None = None) -> str:
    """The shipped health fragment plus the minimum preamble it reads.

    *handle* switches the fragment into --handle-json mode and supplies the health
    code the caller claims to have read. The stub CLI stays on PATH in that mode
    deliberately: it logs every call, so a test can assert the phase reached for no
    pod verb at all rather than merely that the verdict looked right.
    """
    body = _fragment(HEALTH_FRAGMENT_START, HEALTH_FRAGMENT_END)
    preamble = textwrap.dedent(f"""
        set -uo pipefail
        export POD_E2E_HEALTH_TIMEOUT='{timeout}'
        KIROCREW_CLI="{stub_bin}"
        HANDLE_JSON="{'' if handle is None else '/handle/from/the/caller.json'}"
        HANDLE_HEALTH="{'' if handle is None else handle}"
        NAME=demo
        BASE_URL=http://127.0.0.1:7811
        PORT=7811
        # Under the supplied HOME (pytest's tmp_path), never `mktemp -d`: `_run`
        # passes only HOME and PATH, so a bare mktemp would land in the host temp
        # root and stay there after the test.
        ARTIFACT_DIR="$HOME/artifacts"
        mkdir -p "$ARTIFACT_DIR"
        """)
    return (
        preamble
        + _real_reporters()
        # Silence the logger only. The reporters have to stay real: what they record
        # is what the assertions below read.
        + "\nlog() { :; }\n"
        + body
        + '\necho "HEALTHY=$HEALTHY FOREIGN=$FOREIGN"\n'
        + _RECORDED_TAIL
    )


@pytest.fixture()
def stub_cli(tmp_path: Path):
    """A fake `kirocrew` whose `pod status --json` health value is injectable.

    Every invocation is appended to ``cli-calls.log`` beside it, which is how a
    handle-mode test proves no pod verb ran instead of inferring it.
    """

    def _make(health: str) -> str:
        path = tmp_path / "kirocrew-stub"
        calls = tmp_path / "cli-calls.log"
        path.write_text(
            textwrap.dedent(f"""
                #!/usr/bin/env bash
                echo "$@" >> '{calls}'
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
        assert "FAILURES=0" in res.stdout, f"code {code} must not fail: {res.stdout}"


def test_health_refuses_a_foreign_port_holder_and_names_the_conflict(stub_cli, tmp_path):
    """-2 is a squatter, so the phase must NOT proceed, and must say why.

    Blaming the worktree build here is what sent an operator to read a journal
    that only says "address already in use"; the remedy is a free PORT=.
    """
    res = _run(_health_snippet(stub_cli("-2")), str(tmp_path))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "HEALTHY=0 FOREIGN=1" in res.stdout, res.stdout
    assert "FAILURES=1" in res.stdout, res.stdout
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
    assert "FAILURES=0" in res.stdout, res.stdout


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


# --------------------------------------------------------------------------
# --handle-json: run the harness against a pod somebody else started
#
# Every pod verb needs the systemd user bus, so on a host whose outer sandbox
# denies it the harness could not run at all -- the phases that need no bus (auth,
# Playwright, artifacts) were unreachable behind a first phase that did. Handle
# mode supplies the payload instead of asking a verb for it. What these tests pin
# is the part that is easy to get wrong: not that the verdict reads right, but
# that no pod verb is reached, that a bad handle fails at the door rather than
# three phases later, and that the mode does not sit out a poll deadline for a
# code that cannot change while it waits.
# --------------------------------------------------------------------------


def _parse_argv(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess:
    """Parse *argv* with the real loop and report what it captured."""
    snippet = "\n".join(
        [
            "set -uo pipefail",
            ARGS,
            'printf "NAME=%s\\nHANDLE_JSON=%s\\n" "$NAME" "$HANDLE_JSON"',
        ]
    )
    return subprocess.run(
        ["bash", "-c", snippet, "pod-e2e.sh", *argv],
        cwd=tmp_path,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        encoding="utf-8",
    )


@pytest.mark.parametrize("form", ["space", "equals"])
def test_handle_json_is_captured_in_either_spelling(tmp_path, form):
    """Both spellings, because the catch-all has twice rejected a real flag.

    `--fe-only` and `--no-suppress-first-run` each shipped documented and
    unparseable; a value-taking flag doubles the ways to write it, so both reach
    the same variable rather than one of them exiting 64.
    """
    path = "/handle/from/the/caller.json"
    argv = (
        ("smoke", "--handle-json", path) if form == "space" else ("smoke", f"--handle-json={path}")
    )
    res = _parse_argv(tmp_path, *argv)
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"HANDLE_JSON={path}" in res.stdout, res.stdout
    # The value must not be mistaken for the worktree NAME by the `*)` arm.
    assert "NAME=smoke" in res.stdout, res.stdout


def test_handle_json_before_the_name_still_finds_the_name(tmp_path):
    """Order-independence: the value is consumed, not left to become NAME."""
    res = _parse_argv(tmp_path, "--handle-json", "/h.json", "smoke")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "NAME=smoke" in res.stdout, res.stdout


@pytest.mark.parametrize("argv", [("smoke", "--handle-json="), ("smoke", "--handle-json", "")])
def test_empty_handle_json_value_is_refused(tmp_path, argv):
    """An empty handle must never select the destructive lifecycle path."""
    res = _parse_argv(tmp_path, *argv)
    assert res.returncode == 64, res.stdout + res.stderr
    assert "--handle-json needs a non-empty path" in res.stderr, res.stderr
    assert "NAME=" not in res.stdout


def test_handle_json_without_a_value_is_refused(tmp_path):
    """A swallowed NAME would run the harness against the wrong worktree.

    With no value the old `for` loop had no next word to take; a naive shift-less
    port would silently treat the following argument -- the worktree name -- as
    the handle path and then fail much later with an empty base_url.
    """
    res = _parse_argv(tmp_path, "--handle-json")
    assert res.returncode == 64, res.stdout + res.stderr
    assert "needs a path" in res.stderr, res.stderr


# ---------------------------------------------------------------------------
# the door check
# ---------------------------------------------------------------------------


def _fragment_or_fail(start: str, end: str) -> str:
    """Slice at CALL time, so a drifted anchor fails these tests, not collection.

    A module-level slice turns an anchor change into an import error for the whole
    file. That reads as an unrelated crash, and it once hid a real defect: the
    health fragment was being reconstructed one `fi` short, and the module died on
    an unbound variable long before anything noticed the truncation.
    """
    try:
        return _fragment(start, end)
    except ValueError:
        raise AssertionError(
            f"the shipped script no longer contains {start!r} ... {end!r}"
        ) from None


def _real_reporters() -> str:
    """The shipped counters and reporters, not a stand-in written from memory.

    A stub that only echoed its argument left RESULTS empty, and a sliced fragment
    that prints the summary then expanded an empty array: bash 4.4+ permits that
    under `set -u`, but bash 3.2 -- what macOS ships -- calls it unbound and exits
    127 with a message about the wrong thing. Taking the real definitions keeps the
    recording and the counters a fragment reads, so that divergence cannot return.
    """
    counters = _fragment_or_fail("FAILURES=0", "declare -a RESULTS=()")
    reporters = _fragment_or_fail("pass() { RESULTS+=", "WARNINGS=$((WARNINGS + 1)); }")
    return counters + "\n" + reporters


# What the real reporters recorded, made visible to an assertion. The `+` form is
# the empty-array expansion bash 3.2 accepts under `set -u`, so a test asserting
# that NOTHING was recorded reads an empty dump instead of crashing the shell.
_RECORDED_TAIL = (
    '\necho "=== RECORDED ==="\n'
    'printf "%s\\n" ${RESULTS[@]+"${RESULTS[@]}"}\n'
    'echo "FAILURES=$FAILURES WARNINGS=$WARNINGS"\n'
)


def _recorded(res: subprocess.CompletedProcess) -> str:
    """Only what the reporters recorded. A line merely printed lands above this."""
    return res.stdout.split("=== RECORDED ===", 1)[-1]


def _check_handle(
    tmp_path: Path, path: str, live_port: str | None = None
) -> subprocess.CompletedProcess:
    check = _fragment_or_fail("# A bad handle must fail HERE", "  exit 64\n  fi\nfi")
    preamble = ["set -uo pipefail", 'NAME="smoke"', f'HANDLE_JSON="{path}"']
    if live_port is not None:
        # shlex.quote, not repr: repr renders a tab as the two characters `\t`, so a
        # control-character case would reach the shell as different bytes than the
        # producer saw and the comparison would be against a value nobody set.
        preamble.append(f"export KIROCREW_POD_LIVE_PORT={shlex.quote(live_port)}")
    snippet = "\n".join([*preamble, check, 'echo "ACCEPTED"'])
    return _run(snippet, str(tmp_path))


def test_handle_refuses_a_name_that_differs_from_the_argument(tmp_path):
    handle = tmp_path / "handle.json"
    handle.write_text(
        '{"name":"other-pod","base_url":"http://127.0.0.1:7811",'
        '"token":"t","port":7811,"health":200}',
        encoding="utf-8",
    )
    res = _check_handle(tmp_path, str(handle))
    assert res.returncode == 64, res.stdout + res.stderr
    assert "handle name 'other-pod' does not match requested pod 'smoke'" in res.stderr
    assert "ACCEPTED" not in res.stdout


def test_handle_refuses_a_missing_name(tmp_path):
    handle = tmp_path / "handle.json"
    handle.write_text(
        '{"base_url":"http://127.0.0.1:7811","token":"t","port":7811,"health":200}',
        encoding="utf-8",
    )
    res = _check_handle(tmp_path, str(handle))
    assert res.returncode == 64, res.stdout + res.stderr
    assert "name must be a non-empty string" in res.stderr
    assert "ACCEPTED" not in res.stdout


@pytest.mark.parametrize("name", ["", 0, None])
def test_handle_refuses_a_name_that_is_not_a_non_empty_string(tmp_path, name):
    handle = tmp_path / "handle.json"
    handle.write_text(
        json.dumps(
            {
                "name": name,
                "base_url": "http://127.0.0.1:7811",
                "token": "t",
                "port": 7811,
                "health": 200,
            }
        ),
        encoding="utf-8",
    )
    res = _check_handle(tmp_path, str(handle))
    assert res.returncode == 64, res.stdout + res.stderr
    assert "name must be a non-empty string" in res.stderr
    assert "ACCEPTED" not in res.stdout


def test_handle_accepts_a_name_that_matches_the_argument(tmp_path):
    handle = tmp_path / "handle.json"
    handle.write_text(
        '{"name":"smoke","base_url":"http://127.0.0.1:7811",'
        '"token":"t","port":7811,"health":200}',
        encoding="utf-8",
    )
    res = _check_handle(tmp_path, str(handle))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "ACCEPTED" in res.stdout


def test_handle_refuses_a_missing_health_at_the_door(tmp_path):
    handle = tmp_path / "handle.json"
    handle.write_text(
        '{"name":"smoke","base_url":"http://127.0.0.1:7811",' '"token":"t","port":7811}',
        encoding="utf-8",
    )
    res = _check_handle(tmp_path, str(handle))
    assert res.returncode == 64, res.stdout + res.stderr
    assert "health is required" in res.stderr
    assert "ACCEPTED" not in res.stdout


@pytest.mark.parametrize(
    "payload,expected",
    [
        (
            '{"name": "smoke", "base_url": "http://127.0.0.1:7811", "token": "t", "port": 7811, "health": 200}',
            None,
        ),
        # A hand-copied handle may quote the port; localhost is the same host.
        (
            '{"name": "smoke", "base_url": "http://localhost:7811", "token": "t", "port": "7811", "health": 200}',
            None,
        ),
        (
            '{"name": "smoke", "base_url": "http://[::1]:7811", "token": "Az09-_.~+/=", "port": 7811, "health": 200}',
            None,
        ),
        ("not json at all", "not readable JSON"),
        ('["base_url"]', "not a JSON object"),
        (
            '{"name": "smoke", "base_url": "http://127.0.0.1:7811"}',
            "needs a non-empty base_url and token",
        ),
        ('{"token": "t"}', "needs a non-empty base_url and token"),
        ('{"name": "smoke", "base_url": "", "token": ""}', "needs a non-empty base_url and token"),
        (
            '{"name": "smoke", "base_url": "http://127.0.0.1:5476", "token": "t", "port": "05476", "health": 200}',
            "port must be a canonical decimal integer in 1..65535",
        ),
        (
            '{"name": "smoke", "base_url": "http://127.0.0.1:7811", "token": "t", "port": " 7811", "health": 200}',
            "port must be a canonical decimal integer in 1..65535",
        ),
        (
            '{"name": "smoke", "base_url": "http://127.0.0.1:7811", "token": "t", "port": "+7811", "health": 200}',
            "port must be a canonical decimal integer in 1..65535",
        ),
        (
            '{"name": "smoke", "base_url": "http://127.0.0.1:7811", "token": "t", "port": 7811.0, "health": 200}',
            "port must be a canonical decimal integer in 1..65535",
        ),
        (
            '{"name": "smoke", "base_url": "http://127.0.0.1:1", "token": "t", "port": 0, "health": 200}',
            "port must be a canonical decimal integer in 1..65535",
        ),
        (
            '{"name": "smoke", "base_url": "http://127.0.0.1:65535", "token": "t", "port": 65536, "health": 200}',
            "port must be a canonical decimal integer in 1..65535",
        ),
        (
            '{"name": "smoke", "base_url": "http://127.0.0.1:5476", "token": "t", "port": 5476, "health": 200}',
            "base_url port 5476 is the configured live plane",
        ),
        (
            '{"name": "smoke", "base_url": "http://127.0.0.1:7777", "token": "t", "port": 7777, "health": 200}',
            "base_url port 7777 is reserved for a production gateway",
        ),
        (
            '{"name": "smoke", "base_url": "http://127.0.0.1:7812", "token": "t", "port": 7811, "health": 200}',
            "does not match base_url port",
        ),
        (
            '{"name": "smoke", "base_url": "http://127.0.0.1:7811", "token": "t"}',
            "canonical decimal",
        ),
        (
            '{"name": "smoke", "base_url": "http://127.0.0.1:7811", "token": "bad\\nnext", "port": 7811, "health": 200}',
            "token contains whitespace or a control character",
        ),
        (
            '{"name": "smoke", "base_url": "http://127.0.0.1:7811", "token": "bad:token", "port": 7811, "health": 200}',
            "token contains characters outside the generated token format",
        ),
        (
            '{"name": "smoke", "base_url": "http://127.0.0.1:7811/ bad", "token": "t", "port": 7811, "health": 200}',
            "base_url contains whitespace or a control character",
        ),
        # A pod is never remote, so a handle may not send the run off-host.
        (
            '{"name": "smoke", "base_url": "http://10.0.0.5:7811", "token": "t", "port": 7811, "health": 200}',
            "is not loopback",
        ),
        (
            '{"name": "smoke", "base_url": "http://127.0.0.1", "token": "t", "port": 7811, "health": 200}',
            "explicit port",
        ),
        (
            '{"name": "smoke", "base_url": "https://127.0.0.1:7811", "token": "t", "port": 7811, "health": 200}',
            "must be http",
        ),
    ],
)
def test_a_bad_handle_is_refused_at_the_door(tmp_path, payload, expected):
    """Rejecting late reads as a pod that failed to boot.

    The shape errors, left to be caught where the payload is consumed, surface as
    "could not determine base_url" -- which sends the reader to inspect a pod that
    is running perfectly well. The port rules are here for a different reason: the
    production-port refusal downstream reads `port`, while every request the run
    makes reads `base_url`, so those two fields disagreeing is how this harness
    ends up pointed at the live gateway it promises never to touch.
    """
    handle = tmp_path / "handle.json"
    handle.write_text(payload, encoding="utf-8")
    res = _check_handle(tmp_path, str(handle))
    if expected is None:
        assert res.returncode == 0, res.stdout + res.stderr
        assert "ACCEPTED" in res.stdout, res.stdout
    else:
        assert res.returncode == 64, res.stdout + res.stderr
        assert expected in res.stderr, res.stderr


def test_handle_refuses_the_configured_live_port(tmp_path):
    handle = tmp_path / "handle.json"
    handle.write_text(
        '{"name": "smoke", "base_url":"http://127.0.0.1:9123","token":"t","port":9123,"health":200}',
        encoding="utf-8",
    )
    res = _check_handle(tmp_path, str(handle), live_port="9123")
    assert res.returncode == 64, res.stdout + res.stderr
    assert "base_url port 9123 is the configured live plane" in res.stderr
    assert "ACCEPTED" not in res.stdout


def test_handle_refuses_the_default_live_port_when_unset(tmp_path):
    handle = tmp_path / "handle.json"
    handle.write_text(
        '{"name": "smoke", "base_url":"http://127.0.0.1:5476","token":"t","port":5476,"health":200}',
        encoding="utf-8",
    )
    res = _check_handle(tmp_path, str(handle))
    assert res.returncode == 64, res.stdout + res.stderr
    assert "base_url port 5476 is the configured live plane" in res.stderr
    assert "ignoring KIROCREW_POD_LIVE_PORT" not in res.stderr


def test_handle_accepts_a_pod_port_that_is_not_the_configured_live_port(tmp_path):
    handle = tmp_path / "handle.json"
    handle.write_text(
        '{"name": "smoke", "base_url":"http://127.0.0.1:7811","token":"t","port":7811,"health":200}',
        encoding="utf-8",
    )
    res = _check_handle(tmp_path, str(handle), live_port="9123")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "ACCEPTED" in res.stdout
    assert "ignoring KIROCREW_POD_LIVE_PORT" not in res.stderr


@pytest.mark.parametrize("bad", ["abc", "0", "65536", "999999999999999999999999"])
def test_bad_live_port_falls_back_to_the_default_and_warns(tmp_path, bad):
    handle = tmp_path / "handle.json"
    handle.write_text(
        '{"name": "smoke", "base_url":"http://127.0.0.1:5476","token":"t","port":5476,"health":200}',
        encoding="utf-8",
    )
    res = _check_handle(tmp_path, str(handle), live_port=bad)
    assert res.returncode == 64, res.stdout + res.stderr
    assert "ignoring KIROCREW_POD_LIVE_PORT" in res.stderr
    assert "using 5476" in res.stderr
    assert "base_url port 5476 is the configured live plane" in res.stderr
    assert "ACCEPTED" not in res.stdout


@pytest.mark.parametrize(
    "raw",
    [
        " 9123 ",
        "\t9123",
        "9123\n",
        "  9123  ",
        "9123",
        "05476",
        " 5476 ",
        "\uff19\uff11\uff12\uff13",
        "\u0669\u0661\u0662\u0663",
    ],
)
def test_the_refused_set_follows_the_producers_own_parse_of_the_variable(tmp_path, raw):
    """The harness must resolve this variable exactly as the GATEWAY resolves it.

    `_env_int` in `pod/config.py` strips the value and tests it with `str.isdigit()`,
    which spans every Unicode decimal digit, so `' 9123 '` and the fullwidth
    `'\uff19\uff11\uff12\uff13'` both put the real live plane on 9123. A harness that
    resolved either differently would leave 9123 out of the refused set and hand a
    handle on the live gateway to the auth check and Playwright -- the one invariant
    handle mode exists to keep. Pinning the agreement rather than one spelling is what
    makes a future divergence in either parse fail here.
    """
    from kiro_crew.pod.config import DEFAULT_LIVE_PORT, _env_int

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("KIROCREW_POD_LIVE_PORT", raw)
        producer_port = _env_int("KIROCREW_POD_LIVE_PORT", DEFAULT_LIVE_PORT)
    assert 1 <= producer_port <= 65535, "matrix must only carry bindable ports"

    handle = tmp_path / "handle.json"
    handle.write_text(
        json.dumps(
            {
                "name": "smoke",
                "base_url": f"http://127.0.0.1:{producer_port}",
                "token": "t",
                "port": producer_port,
                "health": 200,
            }
        ),
        encoding="utf-8",
    )
    res = _check_handle(tmp_path, str(handle), live_port=raw)
    assert res.returncode == 64, res.stdout + res.stderr
    assert f"port {producer_port} is" in res.stderr
    assert "ACCEPTED" not in res.stdout


def test_handle_refuses_the_default_live_port_even_when_another_is_configured(tmp_path):
    """Resolving a custom live port must ADD to the refused set, not replace it.

    The variable is read from the harness's own environment, so on a deployment that
    exports it only in the gateway's unit the harness sees the default while the
    gateway really is on 5476. Dropping the default there would hand a handle on the
    live plane straight to the auth check and Playwright.
    """
    handle = tmp_path / "handle.json"
    handle.write_text(
        '{"name": "smoke", "base_url":"http://127.0.0.1:5476","token":"t","port":5476,"health":200}',
        encoding="utf-8",
    )
    res = _check_handle(tmp_path, str(handle), live_port="9123")
    assert res.returncode == 64, res.stdout + res.stderr
    assert "base_url port 5476 is reserved for a production gateway" in res.stderr
    assert "ignoring KIROCREW_POD_LIVE_PORT" not in res.stderr
    assert "ACCEPTED" not in res.stdout


def test_a_missing_handle_file_is_refused_by_name(tmp_path):
    res = _check_handle(tmp_path, str(tmp_path / "nope.json"))
    assert res.returncode == 64, res.stdout + res.stderr
    assert "not found" in res.stderr, res.stderr


def _run_handle_through_auth(tmp_path: Path, payload: str) -> subprocess.CompletedProcess:
    """Drive the real door, canonical consumer, port refusal, and auth phase."""
    handle = tmp_path / "handle.json"
    handle.write_text(payload, encoding="utf-8")
    bindir = tmp_path / "curl-shim"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(
        textwrap.dedent("""\
            #!/usr/bin/env bash
            if [[ " $* " == *" --config - "* ]]; then
              cat > "$HOME/curl-config"
              printf 200
            else
              printf '%s\n' "$*" > "$HOME/curl-plain"
              printf 403
            fi
            """),
        encoding="utf-8",
    )
    curl.chmod(0o755)
    door = _fragment_or_fail("# A bad handle must fail HERE", "  exit 64\n  fi\nfi")
    consumer = "\n".join(
        [
            _fragment_or_fail('POD_JSON="$CANONICAL_HANDLE_JSON"', "ALREADY_UP=1"),
            _fragment_or_fail(
                'BASE_URL=$(echo "$POD_JSON"',
                'HANDLE_HEALTH=$(echo "$POD_JSON" | python3 -c "import sys,json; '
                "print(json.load(sys.stdin).get('health',''))\" 2>/dev/null)",
            ),
        ]
    )
    refusal = _fragment_or_fail(
        "# Safety: refuse if port resolves to the production port",
        '  echo "ARTIFACT_DIR=$ARTIFACT_DIR"; exit 1\nfi',
    )
    auth = _fragment_or_fail(
        "# ---------------------------------------------------------------- auth ----",
        '    fail "auth — expected 200/403, got $AUTH_OK/$AUTH_NO"\n  fi\nfi',
    )
    snippet = "\n".join(
        [
            "set -uo pipefail",
            'NAME="smoke"',
            f'HANDLE_JSON="{handle}"',
            door,
            consumer,
            'ARTIFACT_DIR="$HOME/artifacts"',
            _real_reporters(),
            "HEALTHY=1",
            refusal,
            auth,
            'printf "USED:%s|%s|%s\\n" "$BASE_URL" "$TOKEN" "$PORT"',
            _RECORDED_TAIL,
        ]
    )
    return _run(snippet, str(tmp_path), extra_path=str(bindir))


def test_noncanonical_port_never_reaches_the_auth_phase(tmp_path):
    res = _run_handle_through_auth(
        tmp_path,
        '{"name": "smoke", "base_url":"http://127.0.0.1:5476","token":"t","port":"05476","health":200}',
    )
    assert res.returncode == 64, res.stdout + res.stderr
    assert "canonical decimal integer" in res.stderr, res.stderr
    assert "USED:" not in res.stdout
    assert not (tmp_path / "curl-config").exists()


def test_auth_uses_the_canonical_handle_and_drops_a_url_path(tmp_path):
    res = _run_handle_through_auth(
        tmp_path,
        '{"name": "smoke", "base_url":"http://127.0.0.1:7811/trailing","token":"Az09-_.~+/=",'
        '"port":"7811","health":200}',
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "200 with token, 403 without" in _recorded(res), res.stdout
    assert "USED:http://127.0.0.1:7811|Az09-_.~+/=|7811" in res.stdout
    curl_config = (tmp_path / "curl-config").read_text(encoding="utf-8")
    assert curl_config == ('url = "http://127.0.0.1:7811/api/sessions?token=Az09-_.~+/="\n')
    assert "/trailing" not in curl_config


def _run_safety_refusal(
    tmp_path: Path, port: int, live_port: int | None = None
) -> subprocess.CompletedProcess:
    definition = _fragment_or_fail(
        "DEFAULT_LIVE_PORT=5476",
        'HANDLE_REFUSED_PORTS=("$CONFIGURED_LIVE_PORT" "$DEFAULT_LIVE_PORT" 7777)',
    )
    refusal = _fragment_or_fail(
        "# Safety: refuse if port resolves to the production port",
        '  echo "ARTIFACT_DIR=$ARTIFACT_DIR"; exit 1\nfi',
    )
    preamble = [
        "set -uo pipefail",
        f"PORT={port}",
        'ARTIFACT_DIR="$HOME/artifacts"',
        _real_reporters(),
    ]
    if live_port is not None:
        preamble.append(f"export KIROCREW_POD_LIVE_PORT={live_port}")
    return _run("\n".join([*preamble, definition, refusal, "echo CONTINUED"]), str(tmp_path))


@pytest.mark.parametrize(
    "port,live_port",
    [(5476, None), (7777, None), (9123, 9123), (5476, 9123)],
)
def test_safety_refuses_every_live_plane_port(tmp_path, port, live_port):
    """The late guard stays safe even if a handle bypasses the door check.

    The last case is the one a configured port could have opened: this variable is
    read from the harness's own environment, so a deployment that exports it only in
    the gateway's unit would leave the default 5476 unrefused here if resolving a
    custom port replaced it instead of adding to it.
    """
    res = _run_safety_refusal(tmp_path, port, live_port)
    assert res.returncode == 1, res.stdout + res.stderr
    # Read the SUMMARY section, not all of stdout: the refusal is only proven if the
    # reporter recorded it, and a reporter that merely printed would land above this
    # header. That is the divergence a stub reintroduces.
    summary = res.stdout.split("=== POD-E2E SUMMARY ===", 1)[-1]
    assert f"SAFETY — pod resolved to production port {port}, aborting" in summary
    assert "CONTINUED" not in res.stdout


def test_safety_accepts_a_normal_pod_port(tmp_path):
    res = _run_safety_refusal(tmp_path, 7811, 9123)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "SAFETY" not in res.stdout
    assert "CONTINUED" in res.stdout


def _dict_keys_from_function(
    path: Path, function_name: str, markers: set[str], *, require_spread: bool = False
) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    )
    for node in ast.walk(function):
        if not isinstance(node, ast.Dict):
            continue
        keys = {
            key.value
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        if markers <= keys and (not require_spread or None in node.keys):
            return keys
    raise AssertionError(f"no dict in {path}:{function_name} carries {sorted(markers)}")


def _function_node(path: Path, function_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    )


def _door_tree() -> ast.Module:
    source = SCRIPT.read_text(encoding="utf-8")
    start_marker = "CANONICAL_HANDLE_JSON=$(python3 -c '\n"
    end_marker = '\n\' "$HANDLE_JSON" "$NAME" "${HANDLE_REFUSED_PORTS[@]}"); then'
    start = source.index(start_marker) + len(start_marker)
    return ast.parse(source[start : source.index(end_marker, start)])


def _door_required_fields() -> set[str]:
    for node in ast.walk(_door_tree()):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "required_fields"
            for target in node.targets
        ):
            continue
        assert isinstance(node.value, ast.Set)
        return {item.value for item in node.value.elts if isinstance(item, ast.Constant)}
    raise AssertionError("handle door has no required_fields set")


def _assigned_value(path: Path, function_name: str, variable: str) -> ast.expr:
    for node in ast.walk(_function_node(path, function_name)):
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == variable for target in node.targets):
            return node.value
    raise AssertionError(f"{path}:{function_name} never assigns {variable}")


def _pod_live_port_contract() -> tuple[int, str]:
    tree = ast.parse(POD_CONFIG.read_text(encoding="utf-8"))
    default_assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "DEFAULT_LIVE_PORT"
            for target in node.targets
        )
    )
    assert isinstance(default_assignment.value, ast.Constant)
    assert isinstance(default_assignment.value.value, int)

    pod_config = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PodConfig"
    )
    load = next(
        node
        for node in pod_config.body
        if isinstance(node, ast.FunctionDef) and node.name == "load"
    )
    constructor = next(
        node
        for node in ast.walk(load)
        if isinstance(node, ast.Call)
        and any(keyword.arg == "live_port" for keyword in node.keywords)
    )
    live_port = next(
        keyword.value for keyword in constructor.keywords if keyword.arg == "live_port"
    )
    assert isinstance(live_port, ast.Call)
    assert isinstance(live_port.func, ast.Name) and live_port.func.id == "_env_int"
    assert len(live_port.args) == 2
    env_name, default_name = live_port.args
    assert isinstance(env_name, ast.Constant) and isinstance(env_name.value, str)
    assert isinstance(default_name, ast.Name) and default_name.id == "DEFAULT_LIVE_PORT"
    return default_assignment.value.value, env_name.value


def _render_simple_fstring(expression: ast.expr, **values: object) -> str:
    assert isinstance(expression, ast.JoinedStr)
    rendered: list[str] = []
    for part in expression.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            rendered.append(part.value)
        elif isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name):
            rendered.append(str(values[part.value.id]))
        else:
            raise AssertionError(f"unsupported producer URL fragment: {ast.dump(part)}")
    return "".join(rendered)


def _door_canonical_base_url(host: str, port: int) -> str:
    for node in ast.walk(_door_tree()):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value == "base_url"):
                continue
            assert isinstance(value, ast.BinOp) and isinstance(value.op, ast.Mod)
            assert isinstance(value.left, ast.Constant) and isinstance(value.left.value, str)
            assert isinstance(value.right, ast.Tuple)
            names = [item.id for item in value.right.elts if isinstance(item, ast.Name)]
            assert names == ["canonical_host", "claimed"]
            return value.left.value % (host, port)
    raise AssertionError("handle door has no canonical base_url output")


def _door_token_pattern() -> str:
    for node in ast.walk(_door_tree()):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "fullmatch"):
            continue
        if not (isinstance(node.args[1], ast.Name) and node.args[1].id == "token"):
            continue
        assert isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
        return node.args[0].value
    raise AssertionError("handle door has no token alphabet check")


def _producer_token_alphabet() -> set[str]:
    runtime = _function_node(POD_RUNTIME, "mint_token")
    assert any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "/api/token/local?ttl=" in node.value
        for node in ast.walk(runtime)
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "token"
        for node in ast.walk(runtime)
    )

    # runtime.mint_token's `json.loads(resp.read()).get("token", "")` reads the
    # local endpoint; api_token_local's `token = generate_token(...)` reaches the
    # repository-visible generator below.
    handler = _function_node(TOKEN_HANDLER, "api_token_local")
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "generate_token"
        for node in ast.walk(handler)
    )
    assert any(
        isinstance(node, ast.Dict)
        and any(
            isinstance(key, ast.Constant)
            and key.value == "token"
            and isinstance(value, ast.Name)
            and value.id == "token"
            for key, value in zip(node.keys, node.values)
        )
        for node in ast.walk(handler)
    )

    # Import the real encoder rather than lifting it out of the source and exec'ing
    # it: exec on AST-built code trips the repo's SAST rule, and a lifted copy would
    # miss any change made outside that one function body. Other suites import this
    # module directly for the same reason.
    from kiro_crew.dashboard import token_auth

    encode = token_auth._b64url_encode

    generator = _function_node(TOKEN_AUTH, "generate_token")
    token_return = next(
        node
        for node in generator.body
        if isinstance(node, ast.Return) and isinstance(node.value, ast.JoinedStr)
    )
    fields = [
        part.value.id
        for part in token_return.value.values
        if isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name)
    ]
    separators = "".join(
        part.value
        for part in token_return.value.values
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
    )
    assert fields == ["encoded_payload", "signature"]
    assert separators == "."

    alphabet = {"."}
    for value in range(1 << 16):
        alphabet.update(encode(value.to_bytes(2, "big")))
    return alphabet


def test_handle_schema_matches_all_producer_key_sets():
    up_keys = _dict_keys_from_function(POD_CLI, "_up", {"name", "base_url", "token", "port"})
    status_keys = _dict_keys_from_function(POD_CLI, "_status", {"health", "port"})
    wrapper_keys = _dict_keys_from_function(
        WORKTREE_OPS, "_pod_status", {"ok"}, require_spread=True
    )
    obtainable = up_keys | status_keys | wrapper_keys

    assert _door_required_fields() <= obtainable
    assert status_keys - up_keys == {"health"}
    assert {"base_url", "token"}.isdisjoint(status_keys)
    assert wrapper_keys == {"ok"}

    # Pin PodConfig.load line 226: live_port=_env_int("KIROCREW_POD_LIVE_PORT", DEFAULT_LIVE_PORT).
    default_live_port, live_port_env = _pod_live_port_contract()
    shell_source = SCRIPT.read_text(encoding="utf-8")
    shell_default = re.search(r"^DEFAULT_LIVE_PORT=([0-9]+)$", shell_source, re.MULTILINE)
    shell_env = re.search(r'^raw = os\.environ\.get\("([A-Z0-9_]+)"\)$', shell_source, re.MULTILINE)
    assert shell_default is not None
    assert shell_env is not None
    assert int(shell_default.group(1)) == default_live_port
    assert shell_env.group(1) == live_port_env

    # The harness resolves that variable by evaluating the producer's own predicate
    # rather than reimplementing it, which is what makes the two parses agree by
    # construction instead of by review. Pin the shared expression: a change to
    # `_env_int` has to be carried here in the same commit.
    env_int_src = ast.get_source_segment(
        POD_CONFIG.read_text(encoding="utf-8"),
        next(
            node
            for node in ast.walk(ast.parse(POD_CONFIG.read_text(encoding="utf-8")))
            if isinstance(node, ast.FunctionDef) and node.name == "_env_int"
        ),
    )
    assert env_int_src is not None
    for expression in ('val.strip().lstrip("-").isdigit()', "int(val.strip())"):
        producer_expression = expression
        harness_expression = expression.replace("val", "raw")
        assert producer_expression in env_int_src, producer_expression
        assert harness_expression in shell_source, harness_expression

    port = 7811
    producer_url = _render_simple_fstring(_assigned_value(POD_CLI, "_up", "base"), port=port)
    assert producer_url == _door_canonical_base_url("127.0.0.1", port)
    assert producer_url == "http://127.0.0.1:7811"

    pattern = _door_token_pattern()
    door_alphabet = {char for char in string.printable if re.fullmatch(pattern, char)}
    assert door_alphabet == set(string.ascii_letters + string.digits + "-._~+/=")
    assert _producer_token_alphabet() <= door_alphabet


# ---------------------------------------------------------------------------
# health, in handle mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["200", "401", "403"])
def test_a_supplied_healthy_code_is_taken_and_no_pod_verb_runs(stub_cli, tmp_path, code):
    """The stub reports 0, so a healthy verdict can only come from the handle.

    Both halves matter: the code is honoured, AND the phase reaches for no pod
    verb -- which is the whole reason this mode exists, since every verb needs the
    bus this host denies.
    """
    res = _run(_health_snippet(stub_cli("0"), handle=code), str(tmp_path))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "HEALTHY=1" in res.stdout, res.stdout
    assert f"health — supplied by the caller (health={code})" in _recorded(res)
    assert not (
        tmp_path / "cli-calls.log"
    ).exists(), f"a pod verb ran: {(tmp_path / 'cli-calls.log').read_text(encoding='utf-8')}"


def test_a_supplied_code_is_reported_rather_than_passing_silently(stub_cli, tmp_path):
    """The polled path records no health row, so silence there means "probed".

    Handle mode must not borrow that meaning: this run was told the pod is
    healthy, and a caller holding a stale handle is testing a pod that has died.
    """
    res = _run(_health_snippet(stub_cli("0"), handle="200"), str(tmp_path))
    assert "not polled here" in res.stdout, res.stdout


def test_a_foreign_holder_reported_by_the_handle_stops_the_run(stub_cli, tmp_path):
    """-2 is a squatter whichever side read it, and the remedy is still a free port."""
    res = _run(_health_snippet(stub_cli("200"), handle="-2"), str(tmp_path))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "HEALTHY=0 FOREIGN=1" in res.stdout, res.stdout
    assert "held by another process" in res.stdout, res.stdout
    assert "PORT=" in res.stdout, res.stdout
    assert not (tmp_path / "cli-calls.log").exists(), "tried to tail a journal"


@pytest.mark.parametrize("code,shown", [("0", "health=0"), ("503", "health=503")])
def test_an_unhealthy_supplied_code_reaches_the_health_verdict(stub_cli, tmp_path, code, shown):
    """A present unhealthy code belongs to the health phase, not the door."""
    handle = tmp_path / "handle.json"
    handle.write_text(
        json.dumps(
            {
                "name": "smoke",
                "base_url": "http://127.0.0.1:7811",
                "token": "t",
                "port": 7811,
                "health": int(code),
            }
        ),
        encoding="utf-8",
    )
    door = _check_handle(tmp_path, str(handle))
    assert door.returncode == 0, door.stdout + door.stderr
    assert "ACCEPTED" in door.stdout

    res = _run(_health_snippet(stub_cli("200"), handle=code), str(tmp_path))
    assert "HEALTHY=0" in res.stdout, res.stdout
    assert shown in res.stdout, res.stdout
    assert "pod_status" in res.stdout, res.stdout
    assert "boot-fail.log" not in res.stdout, res.stdout


def test_handle_mode_answers_at_once_instead_of_sitting_out_the_deadline(stub_cli, tmp_path):
    """A supplied code is a fact read once, not a state that changes while we sleep.

    Without the loop's handle-mode break an unhealthy handle would burn the whole
    poll deadline -- 60s here, and the default on a real host -- before printing a
    verdict it already had at the first iteration.
    """
    started = time.monotonic()
    res = _run(_health_snippet(stub_cli("200"), timeout="60", handle="0"), str(tmp_path))
    elapsed = time.monotonic() - started
    assert res.returncode == 0, res.stdout + res.stderr
    assert "HEALTHY=0" in res.stdout, res.stdout
    assert elapsed < 15, f"waited {elapsed:.1f}s for a code it was handed"
