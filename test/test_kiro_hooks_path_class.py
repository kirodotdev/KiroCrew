"""The hook-path character class on each platform, checked on every host.

The class is chosen at import from ``platform_compat.IS_WINDOWS``, so the Windows
branch is read off the module source here: a Linux shard checks it too.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

import pytest

from kiro_crew.agent_materialization import kiro_hooks

_SHELL_METACHARACTERS = [
    ";",
    "|",
    "&",
    "$",
    "`",
    '"',
    "'",
    "(",
    ")",
    "<",
    ">",
    "%",
    "!",
    "^",
    "\n",
    "\t",
]


def _branch_patterns() -> tuple[re.Pattern[str], re.Pattern[str]]:
    """(windows, posix) as written in the module, whatever host runs the test."""
    source = Path(kiro_hooks.__file__).read_text(encoding="utf-8")
    (choice,) = [
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.If)
        and ast.unparse(node.test).endswith("IS_WINDOWS")
        and "_SAFE_PATH_RE" in ast.unparse(node)
    ]

    def compiled(body: list[ast.stmt]) -> re.Pattern[str]:
        (assign,) = body
        assert isinstance(assign, ast.Assign)
        assert isinstance(assign.value, ast.Call)
        (pattern,) = assign.value.args
        assert isinstance(pattern, ast.Constant) and isinstance(pattern.value, str)
        return re.compile(pattern.value)

    return compiled(choice.body), compiled(choice.orelse)


WINDOWS, POSIX = _branch_patterns()


@pytest.mark.parametrize(
    "folder",
    [
        pytest.param("Jos\u00e9", id="latin-accent"),
        pytest.param("\u7530\u4e2d", id="cjk"),
        pytest.param("\u0418\u0432\u0430\u043d", id="cyrillic"),
        pytest.param("user\u0663", id="arabic-indic-digit"),
        pytest.param("Jane_Doe", id="ascii-control"),
    ],
)
def test_windows_class_admits_a_profile_folder_of_letters_and_digits(folder: str) -> None:
    assert WINDOWS.match(f"D:\\Profiles\\{folder}\\.kiro\\hooks\\preToolUse.sh")


@pytest.mark.parametrize(
    "char",
    [
        pytest.param(" ", id="space"),
        pytest.param("\u00a0", id="no-break-space"),
        pytest.param("\uff1b", id="fullwidth-semicolon"),
        pytest.param("\uff02", id="fullwidth-quote"),
        pytest.param("\u202e", id="bidi-override"),
        *(pytest.param(c, id=f"U+{ord(c):04X}") for c in _SHELL_METACHARACTERS),
    ],
)
def test_windows_class_still_refuses_the_space_and_shell_metacharacters(char: str) -> None:
    assert not WINDOWS.match(f"D:\\Profiles\\Jos\u00e9{char}x\\.kiro\\hooks\\preToolUse.sh")


def test_posix_class_is_still_ascii_only() -> None:
    assert POSIX.match("/srv/jane_doe/.kiro/hooks/preToolUse.sh")
    assert not POSIX.match("/srv/Jos\u00e9/.kiro/hooks/preToolUse.sh")
    assert not POSIX.match("D:\\Profiles\\Jane_Doe\\hook.sh")


def _hook_in(folder: Path) -> str:
    hook = folder / "preToolUse.sh"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    return str(hook)


def test_validator_under_the_windows_class_loads_an_accented_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kiro_hooks, "_SAFE_PATH_RE", WINDOWS)
    hook = _hook_in(tmp_path / "Jos\u00e9")
    assert kiro_hooks._validate_hook_command(hook, "test") == str(Path(hook).resolve())


def test_refusal_warning_names_the_first_refused_character(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(kiro_hooks, "_SAFE_PATH_RE", WINDOWS)
    hook = _hook_in(tmp_path / "Jane Doe")
    with caplog.at_level(logging.WARNING):
        assert kiro_hooks._validate_hook_command(hook, "test") is None
    (record,) = [r for r in caplog.records if "disallowed characters" in r.getMessage()]
    assert "first refused: ' ' (U+0020)" in record.getMessage()
