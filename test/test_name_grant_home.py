"""A program named ``~/...`` is judged as the path the shell expands it to.

Split from ``test_name_grant.py`` to keep that file under the line target; the
resolution fixtures are shared from there.
"""

from __future__ import annotations

import os

import pytest
from test_name_grant import _clear_pins, _program, world  # noqa: F401

from kiro_crew import name_grant, platform_compat

pytestmark = pytest.mark.skipif(
    platform_compat.IS_WINDOWS,
    reason="resolution fixtures rely on the POSIX execute bit and a ':'-joined PATH",
)


@pytest.mark.usefixtures("_clear_pins", "world")
class TestHomeRelativePrograms:
    """A program named ``~/...`` is judged as the path the shell expands it to.

    ``~/`` never depends on the working directory: the shell replaces it with
    ``HOME`` before it runs anything. So it is not a relative path, and an app
    whose packaged agent spec can only spell its tool as ``~/...`` must get the
    same answer as the absolute spelling -- every absolute-path rule included.
    """

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        home = tmp_path / "userhome"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        monkeypatch.delenv("KIROCREW_OS_HOME", raising=False)
        return home

    def test_a_home_program_outside_the_agent_trees_is_judged_like_its_absolute_path(self, home):
        tool = _program(home / ".local" / "share" / "app" / "bin", "tool")
        refusal = name_grant.name_grant_refusal("~/.local/share/app/bin/tool --version")
        # Not the relative-path refusal: the same witness rule as `tool`'s
        # absolute spelling, which no human has approved yet.
        assert refusal is not None
        assert refusal.code == name_grant.UNWITNESSED
        assert name_grant.name_grant_refusal(f"{tool} --version").code == name_grant.UNWITNESSED

        name_grant.pin_human_approval("~/.local/share/app/bin/tool --version")
        assert name_grant.name_grant_refusal("~/.local/share/app/bin/tool --help") is None
        # One file, one pin, whichever spelling the human approved.
        assert name_grant.name_grant_refusal(f"{tool} --help") is None

    def test_a_home_program_inside_an_agent_tree_is_still_refused(self, home, monkeypatch):
        _program(home / "proj" / "bin", "payload")
        monkeypatch.setattr(
            name_grant, "_agent_writable_roots", lambda: (os.path.normcase(str(home / "proj")),)
        )
        refusal = name_grant.name_grant_refusal("~/proj/bin/payload --help")
        assert refusal is not None
        assert refusal.code == name_grant.AGENT_TREE

    def test_a_home_symlink_into_an_agent_tree_is_still_refused(self, home, tmp_path, monkeypatch):
        checkout = tmp_path / "checkout"
        planted = _program(checkout / "bin", "payload")
        (home / "bin").mkdir()
        (home / "bin" / "tool").symlink_to(planted)
        monkeypatch.setattr(
            name_grant, "_agent_writable_roots", lambda: (os.path.normcase(str(checkout)),)
        )
        refusal = name_grant.name_grant_refusal("~/bin/tool --help")
        assert refusal is not None
        assert refusal.code == name_grant.AGENT_TREE

    @pytest.mark.parametrize(
        "spelling",
        [
            # `~+` is the working directory and `~-` the previous one, which is
            # exactly what the relative-path rule exists to refuse.
            "~+/bin/tool",
            "~-/bin/tool",
            # Another user's home is not this check's to resolve.
            "~root/bin/tool",
        ],
    )
    def test_other_tilde_prefixes_stay_relative(self, home, spelling):
        refusal = name_grant.name_grant_refusal(f"{spelling} --help")
        assert refusal is not None
        assert refusal.code == name_grant.RELATIVE_PATH

    @pytest.mark.parametrize(
        "command",
        [
            # Its own statement: the shell expands the later `~/` against `/x`.
            "HOME=/x; ~/bin/tool",
            "HOME=/x && ~/bin/tool",
            # A prefix: the tool itself reads the moved HOME (git's
            # ~/.gitconfig names programs for git to run).
            "HOME=/x git log",
        ],
    )
    def test_assigning_home_is_a_change_to_what_runs(self, home, command):
        assert name_grant.program_names(command) is None
        assert name_grant.name_grant_refusal(command) is not None

    @pytest.mark.parametrize(
        "spelling",
        # Bash expands only an UNQUOTED, unescaped `~` at the start of a word, so
        # each of these runs `<cwd>/~/bin/tool` -- a file the agent can plant --
        # though `shlex` hands every one of them back as the token `~/bin/tool`.
        ["'~/bin/tool'", "\\~/bin/tool", '"~/bin/tool"', '~"/bin/tool"'],
    )
    def test_a_quoted_or_escaped_tilde_is_not_home(self, home, spelling):
        _program(home / "bin", "tool")
        name_grant.pin_human_approval("~/bin/tool")
        assert name_grant.name_grant_refusal("~/bin/tool --help") is None
        assert name_grant.program_names(f"{spelling} --help") is None
        assert name_grant.name_grant_refusal(f"{spelling} --help") is not None

    def test_a_tilde_inside_an_ansi_c_string_is_not_home(self, home):
        # In `$'...'` a backslash escapes the quote, so `;~/bin/tool` is still
        # inside the string: data, never a HOME program.
        line = "true $'\\';~/bin/tool'"
        assert name_grant._mark_home_tildes(line) == line
        assert name_grant._mark_home_tildes("true;~/bin/tool") == "true;\ue000/bin/tool"

    def test_an_unquoted_tilde_after_an_operator_is_home(self, home):
        _program(home / "bin", "tool")
        name_grant.pin_human_approval("~/bin/tool")
        # Seen as a `~/` program after an operator, so it shares the line with
        # another command and is refused rather than misread as an operand.
        assert name_grant.program_names("cd /tmp && ~/bin/tool x|~/bin/tool") is None
        refusal = name_grant.name_grant_refusal("cd /tmp && ~/bin/tool x|~/bin/tool")
        assert refusal is not None
        assert refusal.code == name_grant.UNTOKENIZABLE
        # An operand spelled `~/...` is data, never a program, and is left alone.
        assert name_grant.program_names("head ~/notes.txt") == ["head"]

    def test_the_reserved_home_marker_in_a_command_refuses(self, home):
        assert name_grant.program_names("head \ue000/x") is None

    def test_a_tilde_after_an_escaped_space_is_mid_word(self, home):
        # `\ ~/x` is ONE word, ` ~/x`, so bash does not expand its `~`.
        _program(home / "bin", "tool")
        name_grant.pin_human_approval("~/bin/tool")
        refusal = name_grant.name_grant_refusal("\\ ~/bin/tool --help")
        assert refusal is not None
        assert refusal.code == name_grant.RELATIVE_PATH

    def test_a_home_spelled_shebang_interpreter_stays_relative(self, home, request):
        # The kernel and `env` do not expand `~`: `#!/usr/bin/env ~/bin/python`
        # runs `<cwd>/~/bin/python`, a file the agent can plant, so judging the
        # HOME file instead would vouch for a program that never runs.
        system_dir, user_dir = request.getfixturevalue("world")
        _program(home / "bin", "python")
        system_env = _program(system_dir, "env")
        script = user_dir / "tool"
        script.write_text(f"#!{system_env} ~/bin/python\n")
        script.chmod(0o700)
        name_grant.pin_human_approval("tool run")
        refusal = name_grant.name_grant_refusal("tool run")
        assert refusal is not None
        assert refusal.code == name_grant.RELATIVE_PATH

    def test_a_pod_child_whose_home_may_be_remapped_is_refused(self, home, tmp_path, monkeypatch):
        # A pod remaps a kiro-cli child's HOME to KIROCREW_OS_HOME, so the
        # gateway's own HOME does not say what the child's shell expands `~/` to.
        _program(home / "bin", "tool")
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", str(tmp_path / "pod-home"))
        name_grant.pin_human_approval("~/bin/tool")
        refusal = name_grant.name_grant_refusal("~/bin/tool --help")
        assert refusal is not None
        assert refusal.code == name_grant.EXPANDED

    @pytest.mark.parametrize(
        "command",
        [
            # Arithmetic and subscript expansion assign with no `HOME=` word.
            "echo $[HOME=0]; ~/bin/tool",
            "echo ${x[HOME=0]}; ~/bin/tool",
            # A bare assignment statement names no other program, yet runs first.
            "A=$[HOME=0]; ~/bin/tool",
            "A=$[HOME=0] && ~/bin/tool",
            "A=${a[$[HOME=0]]}; ~/bin/tool",
            "x=$[HOME=0] ~/bin/tool",
            "~/bin/tool $[HOME=0]",
            # A literal assignment is harmless but still shares the line.
            "A=0; ~/bin/tool",
            # Another command on the line could move HOME too.
            'test -v "a[HOME=0]"; ~/bin/tool',
            "true {HOME}>/dev/null; ~/bin/tool",
            "cd /tmp && ~/bin/tool",
            "~/bin/tool | head",
            # An earlier line moves HOME just as an earlier statement does.
            "echo $[HOME=0]\n~/bin/tool",
        ],
    )
    def test_a_line_that_is_not_a_lone_literal_command_does_not_vouch(self, home, command):
        _program(home / "bin", "tool")
        name_grant.pin_human_approval("~/bin/tool")
        assert name_grant.name_grant_refusal("~/bin/tool --help") is None
        assert name_grant.program_names(command) is None
        refusal = name_grant.name_grant_refusal(command)
        assert refusal is not None
        assert refusal.code == name_grant.UNTOKENIZABLE
        assert "=0" not in refusal.detail

    def test_approving_a_non_lone_line_pins_nothing(self, home):
        _program(home / "bin", "tool")
        name_grant.pin_human_approval("A=$[HOME=0]; ~/bin/tool")
        refusal = name_grant.name_grant_refusal("~/bin/tool")
        assert refusal is not None
        assert refusal.code == name_grant.UNWITNESSED

    def test_a_home_prefix_assignment_is_refused(self, home):
        # `HOME=/x ~/bin/tool` is refused by _EXEC_ENV_VARS: the tool would read
        # the moved HOME even though bash expands the word against the real one.
        _program(home / "bin", "tool")
        name_grant.pin_human_approval("~/bin/tool")
        refusal = name_grant.name_grant_refusal("HOME=/x ~/bin/tool")
        assert refusal is not None

    @pytest.mark.parametrize(
        "command",
        [
            "~/bin/tool",
            "~/bin/tool --help",
            "~/bin/tool a b",
            "~/bin/tool 'a b' c",
            "~/bin/tool > out",
            "~/bin/tool {HOME}>/dev/null",
        ],
    )
    def test_a_lone_literal_home_program_is_honoured(self, home, command):
        # A single command with no other command, no assignment and no `$`: bash
        # expands its word against the inherited HOME before anything else runs.
        _program(home / "bin", "tool")
        name_grant.pin_human_approval("~/bin/tool")
        assert name_grant.name_grant_refusal(command) is None

    @pytest.mark.parametrize(
        ("command", "names"),
        [
            ("echo $HOME", ["echo"]),
            ("echo $[HOME=0]; head x", ["echo", "head"]),
            ("A=$[HOME=0]; head x", ["head"]),
        ],
    )
    def test_a_line_with_no_home_program_is_unaffected(self, home, command, names):
        # No `~/` program, so the walk answers exactly as it does for any bare name.
        assert name_grant.program_names(command) == names
