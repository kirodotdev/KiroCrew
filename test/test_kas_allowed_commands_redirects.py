"""A spec's ``shell.allowedCommands`` on KAS grants a command that carries plain
redirects, as kiro-cli's v3 engine does, and Crew's floors still judge the files.

kiro-cli takes ``> f``, ``>> f``, ``2>&1``, ``< f`` and the like off a command and
matches what is left: the entry ``echo hi`` runs ``echo hi > out.txt`` with no
prompt. Crew's gate does the same (``allowed_commands.split_redirects``), then
withholds the grant when a file a redirect opens is sensitive or write-protected,
or is relative with no known session directory or inside a chain.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_kas_allowed_commands import _judge, _shell

from conftest import make_dir_link
from kiro_crew.config.paths import data_home
from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, TOOL_DENY
from kiro_crew.platform import allowed_commands
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.allowed_commands import COMPOSITION_NOTICE, Redirect


@pytest.fixture(autouse=True)
def _no_profiles(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


def _in(cwd: Path, command: str):
    event = _shell(command)
    event.spec_write_cwd = str(cwd)
    return event


# What kiro-cli 3.3.0's v3 engine ran under the entry ``echo hi``, here on ``make build``.
_KIRO_CLI_RUNS = [
    "make build > out.txt",
    "make build >> out.txt",
    "make build >out.txt",
    "make build > out.txt 2>&1",
    "make build 2> err.txt",
    "make build &> all.txt",
    "make build < in.txt > out.txt",
    "make build 2>&1",
    "make build >&2",
    "make build > sub/../out.txt",
]


@pytest.mark.parametrize("command", _KIRO_CLI_RUNS)
def test_a_plain_redirect_beside_a_matching_command_runs_without_a_prompt(
    tmp_path: Path, command: str
) -> None:
    verdict = _judge(_in(tmp_path, command), ["make build"])
    assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


def test_an_absolute_target_outside_the_session_directory_is_granted(tmp_path: Path) -> None:
    """kiro-cli does not hold a redirect to its allowedPaths; only Crew's floors
    judge the file."""
    outside = (tmp_path / "elsewhere" / "out.txt").as_posix()
    verdict = _judge(_in(tmp_path / "work", f"make build > {outside}"), ["make build"])
    assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


@pytest.mark.parametrize(
    "command",
    [
        # kiro-cli asks for these too.
        "> out.txt make build",
        "make test > out.txt",
        "make $(whoami) > out.txt",
        "make `whoami` > out.txt",
        # Spellings Crew does not read as one plain file name.
        "make build>out.txt",
        "make build > $HOME/out.txt",
        "make build > ~/out.txt",
        "make build > *.txt",
        "make build > 'a b.txt'",
        "make build > a>b",
        "make build >",
        "make build <<EOF",
        "make build <(ls)",
        "make build 10> out.txt",
        "make build 2>&1|cat",
        # Operator forms outside the short list Crew reads: none is granted.
        "make build >! out.txt",
        "make build >>! out.txt",
        "make build &>! all.txt",
        "make build >&! out.txt",
        "make build >| out.txt",
        "make build <> io.txt",
        "make build << EOF",
        "make build <<< text",
        "make build >(cat)",
        "make build <&0",
        "make build >&3",
        "make build >& out.txt",
        "make build 2>> err.txt",
        "make build 1> out.txt",
        "make build 3> out.txt",
        "make build &>> all.txt",
        "make build> out.txt",
        "make build x>out.txt",
        # A quote, a backslash, a # or a non-ASCII character beside a redirect.
        "make build '>' out.txt",
        'make build ">" out.txt',
        "make build > 'out.txt'",
        "make build \\> out.txt",
        "make build > out.txt # note",
        "make build > out\u00e9.txt",
        # Targets the shell would expand or read as something else.
        "make build > =ls",
        "make build > !out.txt",
        "make build > ^out.txt",
        "make build --% > out.txt",
    ],
)
def test_a_redirect_crew_cannot_read_or_kiro_cli_asks_for_still_asks(
    tmp_path: Path, command: str
) -> None:
    verdict = _judge(_in(tmp_path, command), ["make build"])
    assert verdict.action != TOOL_AUTO_APPROVE


@pytest.mark.parametrize("op", [">!", ">>!", "&>!", ">|"])
def test_a_clobber_redirect_beside_a_wide_entry_still_asks(tmp_path: Path, op: str) -> None:
    """zsh reads ``>!`` as one operator. Read as ``>`` with the target ``!``, the
    real file would stay in the rest as an argument that a wide entry matches."""
    config = (data_home() / "config.json").as_posix()
    verdict = _judge(_in(tmp_path, f"make build {op} {config}"), ["make build .*"])
    assert verdict.action != TOOL_AUTO_APPROVE


def test_a_relative_target_with_no_session_directory_asks() -> None:
    verdict = _judge(_shell("make build > out.txt"), ["make build"])
    assert verdict.action == TOOL_ALLOW
    assert verdict.reason == COMPOSITION_NOTICE


def test_a_relative_target_inside_a_chain_asks(tmp_path: Path) -> None:
    """A ``cd`` earlier in the chain moves the directory the shell opens it in."""
    verdict = _judge(_in(tmp_path, "cd sub && make build > out.txt"), ["cd .*", "make build"])
    assert verdict.action == TOOL_ALLOW


def test_an_absolute_target_inside_a_chain_whose_every_part_matches_is_granted(
    tmp_path: Path,
) -> None:
    target = (tmp_path / "out.txt").as_posix()
    verdict = _judge(_in(tmp_path, f"cd sub && make build > {target}"), ["cd .*", "make build"])
    assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


# ── Crew's floors on the file a redirect opens ────────────────────────────────


def test_a_redirect_into_write_protected_config_is_never_granted(tmp_path: Path) -> None:
    config = data_home() / "config.json"
    verdict = _judge(_in(tmp_path, f"make build > {config}"), ["make build"])
    assert verdict.action != TOOL_AUTO_APPROVE
    assert verdict.tier != "spec-builtin-grant"


def test_a_relative_redirect_into_write_protected_config_is_never_granted() -> None:
    home = data_home()
    home.mkdir(parents=True, exist_ok=True)
    verdict = _judge(_in(home, "make build > config.json"), ["make build"])
    assert verdict.action != TOOL_AUTO_APPROVE


def test_a_redirect_through_a_link_to_write_protected_config_is_never_granted(
    tmp_path: Path,
) -> None:
    home = data_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text("{}", encoding="utf-8")
    make_dir_link(tmp_path / "linkdir", home)
    verdict = _judge(_in(tmp_path, "make build > linkdir/config.json"), ["make build"])
    assert verdict.action != TOOL_AUTO_APPROVE


def test_a_redirect_into_a_sensitive_path_is_never_granted(tmp_path: Path) -> None:
    secret = data_home() / ".env"
    verdict = _judge(_in(tmp_path, f"make build > {secret}"), ["make build"])
    assert verdict.action in (TOOL_ALLOW, TOOL_DENY)
    assert verdict.tier != "spec-builtin-grant"


def test_reading_a_sensitive_path_through_a_redirect_is_never_granted(tmp_path: Path) -> None:
    secret = data_home() / ".env"
    verdict = _judge(_in(tmp_path, f"make build < {secret}"), ["make build"])
    assert verdict.action != TOOL_AUTO_APPROVE


def test_a_denied_command_with_a_redirect_is_still_refused(tmp_path: Path) -> None:
    """The deny rules read the whole command, redirects included, before any grant."""
    command = "rm " + "-rf / > out.txt"
    verdict = _judge(_in(tmp_path, command), [".*"])
    assert verdict.action == TOOL_DENY


def test_a_protected_redirect_says_why_it_asks(tmp_path: Path) -> None:
    config = data_home() / "config.json"
    verdict = _judge(_in(tmp_path, f"make build > {config}"), ["make build"])
    assert (verdict.action, verdict.reason) == (TOOL_ALLOW, COMPOSITION_NOTICE)


# ── denyByDefault reads the same match ────────────────────────────────────────


def test_deny_by_default_lets_a_redirected_listed_command_through(tmp_path: Path) -> None:
    event = _in(tmp_path, "make build > out.txt")
    event.spec_shell_allowlist = ("make build",)
    verdict = _judge(event, ["make build"])
    assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


def test_deny_by_default_refuses_a_redirect_glued_to_the_command(tmp_path: Path) -> None:
    event = _in(tmp_path, "make build>out.txt")
    event.spec_shell_allowlist = ("make build",)
    verdict = _judge(event, ["make build"])
    assert verdict.action == TOOL_DENY
    assert "never match" in verdict.reason


# ── the split itself ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("make build", ("make build", ())),
        ("make > build.log 2>&1", ("make", (Redirect("build.log", True),))),
        (
            "sort < in.txt >> out.txt",
            ("sort", (Redirect("in.txt", False), Redirect("out.txt", True))),
        ),
        ("make 2>&1 && make test", ("make && make test", ())),
        ("make >&2", ("make", ())),
        ("make > a.txt; ls", ("make; ls", (Redirect("a.txt", True),))),
        ("make >out.txt", ("make", (Redirect("out.txt", True),))),
        ("make 2> e.txt &> a.txt", ("make", (Redirect("e.txt", True), Redirect("a.txt", True)))),
        ("make 2>&1; ls", ("make; ls", ())),
    ],
)
def test_split_redirects_takes_off_each_plain_redirect(command: str, expected: tuple) -> None:
    assert allowed_commands.split_redirects(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "> f make",
        "make>f",
        "make > ~/f",
        "make > $D/f",
        "make <<EOF",
        "make > f*",
        "",
        None,
        # Operator forms outside the short list.
        "make >! f",
        "make >>! f",
        "make &>! f",
        "make >&! f",
        "make >| f",
        "make <> f",
        "make << f",
        "make <<< f",
        "make <(ls)",
        "make >(cat)",
        "make <&0",
        "make >&3",
        "make >&f",
        "make 2>> f",
        "make 1> f",
        "make 3> f",
        "make &>> f",
        "make a>b",
        "make > a>b",
        "make >",
        "make >; ls",
        "ls; > f make",
        "ls && > f make",
        # A quote, a backslash, a # or a non-ASCII character beside a redirect.
        "make '>' f",
        'make ">" f',
        "make > 'f'",
        'make > "f"',
        "make \\> f",
        "make > f # c",
        "make > f\u00e9",
        # Targets the shell would expand or read as something else.
        "make > =ls",
        "make > !f",
        "make > ^f",
        "make --% > f",
    ],
)
def test_split_redirects_refuses_what_it_cannot_read(command: object) -> None:
    assert allowed_commands.split_redirects(command) is None
