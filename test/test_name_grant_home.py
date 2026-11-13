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

    def test_an_unquoted_tilde_after_an_operator_is_home(self, home):
        _program(home / "bin", "tool")
        name_grant.pin_human_approval("~/bin/tool")
        assert name_grant.program_names("cd /tmp && ~/bin/tool x|~/bin/tool") == [
            "cd",
            "~/bin/tool",
            "~/bin/tool",
        ]
        assert name_grant.name_grant_refusal("cd /tmp && ~/bin/tool x|~/bin/tool") is None
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
