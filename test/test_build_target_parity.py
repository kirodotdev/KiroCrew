"""Build gate: the Makefile and make.ps1 expose the same target set.

The two build drivers are separate implementations of one contract -- POSIX
contributors run ``make <target>``, Windows contributors run
``.\\make.ps1 <target>`` -- and the documented target table in
``docs/guides/install.md`` describes both. Nothing structural stops one from
gaining a target the other lacks, and the failure mode is silent: a Windows
contributor following the docs gets "unknown target", or a new POSIX target
never reaches Windows at all.

Hermetic -- pure text parsing of the two files, no build executed.

Runs on every platform, deliberately: the Makefile side is what a Windows-only
change is most likely to forget, and the POSIX matrix is where most of the
suite's runs happen.
"""

from __future__ import annotations

import pathlib
import re

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Targets that exist for make's own bookkeeping rather than as a build action a
# user invokes. ``all`` is make's default-goal convention; make.ps1 spells the
# same default as its ``$Target`` parameter default, not as a switch arm.
_MAKE_ONLY_TARGETS = frozenset({"all"})


def _makefile_targets() -> set[str]:
    """Target names from the Makefile's .PHONY declaration.

    .PHONY rather than the rule lines themselves: it is the single line that
    enumerates every target, so a rule added without updating it is already a
    Makefile bug this test surfaces as a parity failure.
    """
    text = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    match = re.search(r"^\.PHONY:(.*)$", text, re.MULTILINE)
    assert match, "Makefile has no .PHONY line to read targets from"
    return set(match.group(1).split())


def _powershell_code() -> str:
    """make.ps1 with comment lines stripped.

    The checks below assert that specific guards are PRESENT in the code. Every
    such guard is also named in a nearby rationale comment, so matching raw file
    text would keep passing after the guard itself was deleted -- the assertion
    has to see code only.
    """
    text = (_REPO_ROOT / "make.ps1").read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _powershell_targets() -> set[str]:
    """Target names from make.ps1's dispatch switch.

    Reads the switch arms (``"build" { ... }``) rather than the function names:
    the arms are what a user can actually type, and a helper function such as
    Invoke-Build is not necessarily a target.
    """
    text = (_REPO_ROOT / "make.ps1").read_text(encoding="utf-8")
    # The switch body runs from `switch ($Target...` to the closing `default`.
    body = text.split("switch ($Target.ToLowerInvariant())", 1)
    assert len(body) == 2, "make.ps1 has no target dispatch switch"
    body_text = body[1].split("default", 1)[0]
    return set(re.findall(r'^\s*"([a-z-]+)"\s*\{', body_text, re.MULTILINE))


def test_makefile_and_powershell_expose_the_same_targets() -> None:
    make_targets = _makefile_targets() - _MAKE_ONLY_TARGETS
    ps_targets = _powershell_targets() - _MAKE_ONLY_TARGETS

    missing_on_windows = sorted(make_targets - ps_targets)
    missing_on_posix = sorted(ps_targets - make_targets)

    assert not missing_on_windows, (
        "Makefile targets with no make.ps1 equivalent: "
        f"{missing_on_windows}. Add them to make.ps1 (or to _MAKE_ONLY_TARGETS "
        "if they are make bookkeeping rather than a user-invocable target)."
    )
    assert not missing_on_posix, (
        f"make.ps1 targets with no Makefile equivalent: {missing_on_posix}. "
        "Add them to the Makefile's .PHONY line and give them a rule."
    )


def test_documented_target_table_covers_every_target() -> None:
    """The install guide's target table names every user-invocable target.

    The table is the only place a contributor learns a target exists, so a
    target absent from it ships undiscoverable.
    """
    doc = (_REPO_ROOT / "docs" / "guides" / "install.md").read_text(encoding="utf-8")
    undocumented = sorted(
        t for t in _makefile_targets() - _MAKE_ONLY_TARGETS if f"make {t}" not in doc
    )
    assert (
        not undocumented
    ), f"targets missing from docs/guides/install.md's target table: {undocumented}"


def _makefile_recipe(target: str) -> str:
    """The recipe body of one Makefile target, with comment lines stripped.

    Same reasoning as ``_powershell_code``: the guards asserted below are named
    in nearby rationale comments, so matching raw text would keep passing after
    the recipe line itself was deleted.
    """
    text = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(target)}:\n((?:\t.*\n)+)", text, re.MULTILINE)
    assert match, f"Makefile has no recipe for target {target!r}"
    return "\n".join(
        line for line in match.group(1).splitlines() if not line.strip().startswith("#")
    )


def test_frontend_target_installs_the_electron_subpackage() -> None:
    """Both build drivers must install website/electron's own dependencies.

    ``website/electron`` is a separate npm package (``website/package.json``
    declares no ``workspaces``), so the website/ install step never reaches it.
    A scripted build that skips it leaves ``npm test`` / ``npm run check`` in
    website/ dying with MODULE_NOT_FOUND on electron's missing deps -- the gap
    that made CONTRIBUTING.md grow a manual work-around note, since dropped
    because the build owns the install (#7226).
    """
    recipe = _makefile_recipe("frontend")
    assert "cd electron" in recipe, (
        "the Makefile frontend recipe no longer enters website/electron; its "
        "deps then go uninstalled and `npm test` in website/ breaks"
    )
    # Bound the window to the electron subshell: matching the rest of the
    # recipe would let unrelated npm lines satisfy the assertion after the
    # electron block itself lost the branch.
    electron_part = recipe.split("cd electron", 1)[1].split(")", 1)[0]
    assert "npm ci" in electron_part and "npm install" in electron_part, (
        "the Makefile frontend recipe must run the ci-vs-install branch inside "
        "website/electron, mirroring the website/ install step"
    )

    code = _powershell_code()
    assert 'Push-Location "electron"' in code, (
        "make.ps1's Invoke-Frontend no longer enters website/electron; its "
        "deps then go uninstalled and `npm test` in website/ breaks on Windows"
    )
    # Same bound: the rest of make.ps1 has pip `"install"` invocations that
    # would keep the assertion green after the electron block lost the branch.
    ps_electron_part = code.split('Push-Location "electron"', 1)[1].split("Pop-Location", 1)[0]
    assert '"ci"' in ps_electron_part and '"install"' in ps_electron_part, (
        "make.ps1's Invoke-Frontend must run the ci-vs-install branch inside "
        "website/electron, mirroring the website/ install step"
    )


def test_powershell_driver_declares_no_posix_only_step() -> None:
    """make.ps1 must not invoke the macOS-only resign step.

    ``packaging/resign-macos-libs.sh`` works around a macOS code-signing
    validation flake and exits 0 immediately off Darwin. Calling it from the
    Windows driver would add a bash dependency to the plain ``backend`` target,
    which is otherwise bash-free -- only the two desktop targets need bash.
    """
    text = (_REPO_ROOT / "make.ps1").read_text(encoding="utf-8")
    assert "resign-macos-libs" not in text.replace(
        "# No resign-macos-libs step", ""
    ), "make.ps1 should not invoke resign-macos-libs.sh"


def test_bash_resolution_rejects_the_wsl_and_store_stubs() -> None:
    """Get-BashPath must not hand the desktop targets a non-MSYS ``bash``.

    ``System32\\bash.exe`` is the WSL launcher, present on any machine with WSL
    installed and ahead of Git Bash on a default PATH. It is the dangerous
    candidate precisely because it RUNS: a liveness probe cannot tell it apart,
    but it executes build-desktop.sh inside a Linux distro, where ``uname``
    reports Linux and the script takes its POSIX branches -- so the Windows PBS
    layout and the electron-builder ``--win`` target never run, and the build
    either fails obscurely or produces a Linux artifact.

    Asserted statically because reproducing it needs WSL installed, which
    neither the CI runners nor most dev machines have.
    """
    code = _powershell_code()
    for stub in ("System32\\bash.exe", "WindowsApps"):
        assert stub in code, f"make.ps1's bash resolution no longer rejects the {stub} stub"


def test_shared_toolchain_marker_is_written_without_a_bom() -> None:
    """The python-bin marker must never be written with ``Set-Content -Encoding utf8``.

    That marker is SHARED with the Makefile, which reads it as
    ``PY="$(cat <data-home>/python-bin)"``. PowerShell 5.1 -- still the
    Windows-shipped default -- writes a UTF-8 BOM for ``-Encoding utf8``, so the
    path the Makefile then executes is prefixed with EF BB BF and fails as "No
    such file or directory". The corruption is cross-driver: a Windows build
    would break the POSIX one on a machine sharing a data home (WSL, or a dual
    checkout).
    """
    code = _powershell_code()
    assert "UTF8Encoding" in code, (
        "make.ps1 must write the shared python-bin marker as BOM-less UTF-8 "
        "(System.Text.UTF8Encoding($false)); PowerShell 5.1's -Encoding utf8 adds a BOM"
    )
    assert "-Encoding utf8" not in code, (
        "make.ps1 writes a file with `-Encoding utf8`, which prepends a UTF-8 BOM "
        "under PowerShell 5.1"
    )


def test_toolchain_markers_are_read_through_the_null_safe_helper() -> None:
    """Marker reads must go through ``Read-Marker``, never a bare ``.Trim()``.

    ``Get-Content`` on a zero-byte file returns ``$null``, and under StrictMode
    ``$null.Trim()`` raises "You cannot call a method on a null-valued
    expression". A truncated marker -- an interrupted ensure-python.sh write, a
    full disk -- would then abort the build outright instead of falling through
    to ordinary toolchain discovery, which is the whole point of the marker
    being an advisory cache.
    """
    code = _powershell_code()
    assert "function Read-Marker" in code, "make.ps1 lost its null-safe marker reader"
    assert "(Get-Content" not in code.replace("([string](Get-Content", ""), (
        "make.ps1 reads a marker without the [string] cast; route it through "
        "Read-Marker so an empty marker cannot throw on .Trim()"
    )


def test_build_steps_are_judged_by_exit_code_not_by_stderr() -> None:
    """Native build steps must run through ``Invoke-Step``.

    npm, pip and electron-builder all report progress and warnings on stderr
    while succeeding, so the exit code is the only correct success signal.
    ``$ErrorActionPreference = "Stop"`` promotes native stderr to a terminating
    error whenever the stream is captured or redirected, which is exactly what a
    CI log capture or an outer redirection does -- so a warning could fail a
    build that worked. ``Invoke-Step`` relaxes the preference around the call and
    then asserts ``$LASTEXITCODE``.

    A bare ``& $tool ...`` followed by a hand-rolled exit-code check is the shape
    this forbids: it is the form that regresses, because it looks correct.
    """
    code = _powershell_code()
    assert "function Invoke-Step" in code, "make.ps1 lost its exit-code-judging step runner"
    # Only Invoke-Step itself may call Assert-LastExitCode; a second caller means
    # some step bypassed the wrapper.
    assert code.count("Assert-LastExitCode") == 2, (
        "every native build step must go through Invoke-Step -- found a call site "
        "asserting $LASTEXITCODE on its own"
    )
