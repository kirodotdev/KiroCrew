"""Regression guard for the bundled Windows ``kirocrew.cmd`` launcher's Python env.

Background
----------
``packaging/build-desktop.sh`` writes the Windows backend bundle's launcher,
``<bundle>\\bin\\kirocrew.cmd``, with a single ``printf``.  Like the POSIX
launcher it must clear an inherited ``PYTHONPATH``/``PYTHONHOME`` before the
bundled ``python.exe`` starts: forwarded as-is, ``kirocrew token`` or ``kirocrew
stop`` run from a development shell imports checkout code mixed into the
shipped backend.  The desktop shell's own scrub
(``website/electron/gateway-env.js``) never runs for these direct invocations,
and on Windows the shell unwraps the ``.cmd`` to ``python.exe`` anyway, so the
``.cmd`` is the one place a direct caller is covered.

Why this test exists
--------------------
There is no Windows interpreter to run the shim against in the Linux suite, and
``cmd.exe`` is not available either, so this renders the ``printf`` format the
build script uses (the real one, extracted; a revert is what runs here) and
asserts the shim's line sequence: the deletions come before the interpreter
line, inside ``setlocal`` so a user's interactive ``cmd`` window keeps its own
variables.
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "packaging" / "build-desktop.sh"
BUILD_WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "build.yml"


def _render_cmd_launcher(subdir: str = "bin") -> list[str]:
    """Extract the ``printf`` format for ``<subdir>/kirocrew.cmd`` and render its lines.

    Rendering mirrors what the shell does with a single-quoted ``printf`` format
    and no arguments: ``%%`` -> ``%``, ``\\\\`` -> ``\\``, ``\\r\\n`` -> CRLF.  The
    shell itself is not invoked so the check runs on every platform.
    """
    # The build script holds non-ASCII text, so the Windows code page cannot
    # decode it: pin the encoding.
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(
        rf"printf '([^']*)' > \"\$out/{subdir}/kirocrew\.cmd\"",
        text,
    )
    assert m, f"{subdir}/kirocrew.cmd printf not found in packaging/build-desktop.sh"
    fmt = m.group(1)
    rendered = fmt.replace("%%", "%").replace("\\\\", "\\").replace("\\r\\n", "\r\n")
    assert rendered.endswith("\r\n"), "every .cmd line must end in CRLF"
    lines = rendered.split("\r\n")[:-1]
    assert all("\n" not in line and "\r" not in line for line in lines), lines
    return lines


def _workflow_cmd_launcher(subdir: str = "bin") -> list[str]:
    """The Windows installer workflow writes its own copy of the shim.

    ``build.yml`` builds the Windows backend in PowerShell and writes
    ``kirocrew.cmd`` with ``Set-Content -Value @('line', ...)``; that copy is
    what the installer smoke actually boots, so it has to carry the same lines
    as the ``printf`` in ``build-desktop.sh``.  Pull the array's single-quoted
    elements out (PowerShell single quotes have no escapes to undo here).
    """
    text = BUILD_WORKFLOW.read_text(encoding="utf-8")
    m = re.search(
        rf"\(Join-Path \$out \"{subdir}\\kirocrew\.cmd\"\) `\s*\n\s*-Value @\(([^\n]*)\)",
        text,
    )
    assert m, f"{subdir}\\kirocrew.cmd Set-Content not found in .github/workflows/build.yml"
    return re.findall(r"'([^']*)'", m.group(1))


def test_bundle_replaces_the_pip_scripts_launcher_in_both_writers():
    """Neither bundle writer may ship pip's ``Scripts\\kirocrew.exe``.

    pip writes that ``.exe`` with the BUILD machine's absolute ``python.exe``
    path in its shebang, so on any installed copy it fails with "Fatal error in
    launcher". Both writers must delete it (and any ``kirocrew-script.py``) and
    put the relocatable shim in its place, with the same lines as ``bin\\``'s:
    ``Scripts\\`` is one level below the bundle root too, so ``%~dp0..`` names
    the same interpreter.
    """
    script = SCRIPT.read_text(encoding="utf-8")
    assert re.search(
        r'rm -f "\$out/Scripts/kirocrew\.exe" "\$out/Scripts/kirocrew-script\.py"', script
    ), "build-desktop.sh must delete pip's Scripts/kirocrew.exe from the bundle"
    workflow = BUILD_WORKFLOW.read_text(encoding="utf-8")
    assert re.search(
        r'Remove-Item [^\n]*`\s*\n\s*-LiteralPath \(Join-Path \$out "Scripts\\kirocrew\.exe"\), '
        r'\(Join-Path \$out "Scripts\\kirocrew-script\.py"\)',
        workflow,
    ), "build.yml must delete pip's Scripts\\kirocrew.exe from the bundle"

    assert _render_cmd_launcher("Scripts") == _render_cmd_launcher("bin")
    assert _workflow_cmd_launcher("Scripts") == _render_cmd_launcher("bin")

    # The shim is written AFTER the deletion, so the removal cannot take it.
    assert script.index('rm -f "$out/Scripts/kirocrew.exe"') < script.index(
        '> "$out/Scripts/kirocrew.cmd"'
    )
    assert workflow.index('"Scripts\\kirocrew-script.py")') < workflow.index(
        '(Join-Path $out "Scripts\\kirocrew.cmd")'
    )


def test_build_workflow_writes_the_same_cmd_launcher():
    """The installer workflow's copy of the shim matches the packaging script's.

    Two writers of one file drift silently: the Windows smoke would keep booting
    the workflow's copy while the packaging script's copy is the one tested
    here.  Holding them to identical lines means a change to either fails until
    the other follows.
    """
    assert _workflow_cmd_launcher() == _render_cmd_launcher()


def test_cmd_launcher_clears_pythonpath_and_pythonhome_before_python_runs():
    """The deletions must precede the interpreter line."""
    lines = _render_cmd_launcher()

    python_idx = next(i for i, line in enumerate(lines) if '"%~dp0..\\python.exe"' in line)
    assert (
        'set "PYTHONPATH="' in lines[:python_idx]
    ), f"kirocrew.cmd still forwards PYTHONPATH to the bundled python.exe (issue #18378): {lines}"
    assert (
        'set "PYTHONHOME="' in lines[:python_idx]
    ), f"kirocrew.cmd still forwards PYTHONHOME to the bundled python.exe (issue #18378): {lines}"


def test_cmd_launcher_scopes_the_deletion_with_setlocal():
    """``setlocal`` keeps the scrub inside the shim.

    Without it, a batch file's ``set`` lands in the calling interactive
    ``cmd.exe``'s environment, so running ``kirocrew`` once would wipe the
    developer's own PYTHONPATH for the rest of their session.
    """
    lines = _render_cmd_launcher()

    assert lines[0] == "@echo off", lines
    first_set = next(i for i, line in enumerate(lines) if line.startswith("set "))
    assert (
        "setlocal" in lines[:first_set]
    ), f"setlocal must come before the first `set` so the scrub does not leak: {lines}"


def test_cmd_launcher_keeps_the_module_invocation_contract():
    """The interpreter line is unchanged: ``-s -P -m kiro_crew %*``.

    ``gateway-recovery.js`` unwraps this shim to the same argv for the desktop
    shell's own spawn, so the two must keep agreeing; and no ``-E`` here either,
    since that would also drop PYTHONUTF8 and the bytecode knobs the shell hands
    down.
    """
    lines = _render_cmd_launcher()

    assert lines[-1] == '"%~dp0..\\python.exe" -s -P -m kiro_crew %*', lines
