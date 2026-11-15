"""cli.sh ends an install whose bin directory is off PATH with the line to paste.

``_path_hint`` is extracted from cli.sh and run alone under ``sh``, with a
``uname`` stub ahead of the system directories so the macOS answer is checked
on any host.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_installer_distro import CLI_SH, _cli_sh_function

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="cli.sh is POSIX only")

BIN = "/home/u/.local/bin"
_ADD = f"{BIN} is not on your PATH yet. To add it, run:\n"


def _rc_hint(rc: str) -> str:
    return (
        f"{_ADD}  printf '\\n%s\\n' 'export PATH=\"{BIN}:$PATH\"' >> {rc}\n"
        "then open a new terminal, or run this in the current one:\n"
        f'  export PATH="{BIN}:$PATH"\n'
    )


@pytest.mark.parametrize(
    ("shell", "system", "home_files", "on_path", "expected"),
    [
        ("/bin/zsh", "Linux", (), False, _rc_hint("~/.zshrc")),
        ("/bin/bash", "Linux", (), False, _rc_hint("~/.bashrc")),
        ("/bin/bash", "Darwin", (), False, _rc_hint("~/.bash_profile")),
        ("/bin/bash", "Darwin", (".profile",), False, _rc_hint("~/.profile")),
        ("/bin/bash", "Darwin", (".profile", ".bash_profile"), False, _rc_hint("~/.bash_profile")),
        ("/usr/bin/fish", "Linux", (), False, f'{_ADD}  fish_add_path "{BIN}"\n'),
        (
            "/bin/tcsh",
            "Linux",
            (),
            False,
            f"Add {BIN} to your PATH first (e.g. add it in your shell profile).\n",
        ),
        ("/bin/zsh", "Linux", (), True, ""),
    ],
    ids=[
        "zsh",
        "bash",
        "bash-macos",
        "bash-macos-profile",
        "bash-macos-first-wins",
        "fish",
        "other",
        "already-on-path",
    ],
)
def test_path_hint_prints_the_line_for_the_users_shell(
    tmp_path: Path,
    shell: str,
    system: str,
    home_files: tuple[str, ...],
    on_path: bool,
    expected: str,
) -> None:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    uname = stubs / "uname"
    uname.write_text(f"#!/bin/sh\necho {system}\n", encoding="utf-8")
    uname.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    for name in home_files:
        (home / name).write_text("", encoding="utf-8")
    hint = tmp_path / "hint.sh"
    hint.write_text(_cli_sh_function("_path_hint"), encoding="utf-8")
    path = [str(stubs), *([BIN] if on_path else []), "/usr/bin", "/bin"]
    result = subprocess.run(
        ["sh", "-c", f'set -eu; . "{hint}"; _path_hint'],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={"PATH": os.pathsep.join(path), "BIN": BIN, "SHELL": shell, "HOME": str(home)},
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


def test_the_pasted_profile_line_does_not_join_an_unterminated_last_line(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    profile = home / ".zshrc"
    profile.write_text("# last line, no newline", encoding="utf-8")
    hint = tmp_path / "hint.sh"
    hint.write_text(_cli_sh_function("_path_hint"), encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "BIN": BIN, "SHELL": "/bin/zsh", "HOME": str(home)}
    shown = subprocess.run(
        ["sh", "-c", f'set -eu; . "{hint}"; _path_hint'],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=tmp_path,
        check=True,
    ).stdout.splitlines()[1]
    subprocess.run(
        ["sh", "-c", shown],
        capture_output=True,
        encoding="utf-8",
        env=env,
        cwd=tmp_path,
        check=True,
    )
    assert profile.read_text(encoding="utf-8").splitlines() == [
        "# last line, no newline",
        f'export PATH="{BIN}:$PATH"',
    ]


def test_the_zsh_hint_names_the_zshrc_in_zdotdir(tmp_path: Path) -> None:
    hint = tmp_path / "hint.sh"
    hint.write_text(_cli_sh_function("_path_hint"), encoding="utf-8")
    zdotdir = str(tmp_path / "zsh")
    result = subprocess.run(
        ["sh", "-c", f'set -eu; . "{hint}"; _path_hint'],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={"PATH": "/usr/bin:/bin", "BIN": BIN, "SHELL": "/bin/zsh", "ZDOTDIR": zdotdir},
        cwd=tmp_path,
        check=True,
    )
    assert result.stdout == _rc_hint(f"{zdotdir}/.zshrc")


def test_the_install_summary_prints_the_path_hint() -> None:
    assert 'echo "Installed kirocrew $VER (channel: $CHANNEL)."\n_path_hint\n' in CLI_SH.read_text()
