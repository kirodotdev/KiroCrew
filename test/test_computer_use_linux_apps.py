"""The Linux launch catalog: root-owned desktop entries only, names only."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew.computer_use.driver_runtime import linux_apps
from kiro_crew.computer_use.types import (
    AmbiguousLaunchTarget,
    ComputerUseError,
    NoSuchLaunchTarget,
)


def _entry(
    name: str, argv: tuple[str, ...], *, terminal: bool = False, desktop_id: str = ""
) -> linux_apps.DesktopEntry:
    return linux_apps.DesktopEntry(
        name=name,
        desktop_id=desktop_id or name.lower(),
        argv=argv,
        terminal_emulator=terminal,
        path="/x",
    )


@pytest.mark.parametrize(
    "exec_line, argv",
    [
        ("gnome-calculator", ("gnome-calculator",)),
        ("firefox %u", ("firefox",)),
        ("gedit %U --new-window", ("gedit", "--new-window")),
        ("app --opt=%f", ("app", "--opt=")),
        ("app 100%%", ("app", "100%")),
        ('"/opt/My App/bin/app" %F', ("/opt/My App/bin/app",)),
        ("broken 'quote", ()),
    ],
)
def test_field_codes_are_removed(exec_line: str, argv: tuple[str, ...]) -> None:
    assert linux_apps.strip_field_codes(exec_line) == argv


def test_parse_entry_skips_hidden_and_marks_terminals(tmp_path: Path) -> None:
    good = tmp_path / "calc.desktop"
    good.write_text("[Desktop Entry]\nType=Application\nName=Calculator\nExec=gnome-calculator\n")
    hidden = tmp_path / "h.desktop"
    hidden.write_text("[Desktop Entry]\nType=Application\nName=H\nExec=h\nNoDisplay=true\n")
    term = tmp_path / "t.desktop"
    term.write_text(
        "[Desktop Entry]\nType=Application\nName=Term\nExec=xterm\nCategories=System;TerminalEmulator;\n"
    )
    assert linux_apps.parse_entry(good, "calc").argv == ("gnome-calculator",)  # type: ignore[union-attr]
    assert linux_apps.parse_entry(hidden, "h") is None
    assert linux_apps.parse_entry(term, "t").terminal_emulator  # type: ignore[union-attr]


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_a_user_writable_applications_dir_is_never_read(tmp_path: Path) -> None:
    (tmp_path / "evil.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Evil\nExec=/tmp/evil\n"
    )
    assert linux_apps.installed_apps(dirs=(str(tmp_path),)) == ()


def test_resolution_exact_prefix_ambiguous_and_near(monkeypatch: pytest.MonkeyPatch) -> None:
    # Program resolution and ownership are pinned separately; this one is about
    # names, so it must not depend on the runner having a POSIX /usr/bin/true.
    monkeypatch.setattr(linux_apps, "resolve_program", lambda program: program)
    catalog = (
        _entry("Calculator", ("/usr/bin/true",), desktop_id="org.gnome.Calculator"),
        _entry("Text Editor", ("/usr/bin/true",)),
        _entry("Text Viewer", ("/usr/bin/true",)),
    )
    target, name = linux_apps.resolve_target("calculator", catalog=catalog)
    assert name == "Calculator" and target.split("\0")[0] == "/usr/bin/true"
    assert linux_apps.resolve_target("org.gnome.calculator", catalog=catalog)[1] == "Calculator"
    assert linux_apps.resolve_target("calc", catalog=catalog)[1] == "Calculator"
    with pytest.raises(AmbiguousLaunchTarget):
        linux_apps.resolve_target("text", catalog=catalog)
    with pytest.raises(NoSuchLaunchTarget) as err:
        linux_apps.resolve_target("editor", catalog=catalog)
    assert "Text Editor" in err.value.near


def test_terminals_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(linux_apps, "_secure_chain", lambda path: True)
    with pytest.raises(ComputerUseError, match="terminal"):
        linux_apps.resolve_target(
            "xterm",
            catalog=(_entry("XTerm", ("/usr/bin/true",), terminal=True, desktop_id="xterm"),),
        )


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_a_program_the_user_can_replace_is_refused(tmp_path: Path) -> None:
    prog = tmp_path / "prog"
    prog.write_text("#!/bin/sh\n")
    prog.chmod(0o755)
    with pytest.raises(ComputerUseError, match="root-owned"):
        linux_apps.resolve_target("prog", catalog=(_entry("Prog", (str(prog),)),))


def test_a_program_missing_from_the_system_path_is_refused() -> None:
    with pytest.raises(ComputerUseError, match="system path"):
        linux_apps.resolve_target(
            "x", catalog=(_entry("X", ("definitely-not-a-real-program-kc",), desktop_id="x"),)
        )
