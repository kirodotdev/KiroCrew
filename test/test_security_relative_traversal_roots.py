"""Relative traversal roots resolve against the command's own ``cd``, never the gateway's cwd.

The structure passes of the bash gate (alternate traversal, ``find`` traversal) used
to resolve a relative root such as ``.`` against this process's working directory.
The agent's shell does not run there, and the desktop app's gateway runs from ``/``,
which holds every fenced store, so every ``grep -r X .`` was refused whatever ``cd``
preceded it. Each "allowed" assertion here runs with the process cwd pinned to a
directory that DOES hold a fenced store, so it fails on the old resolution; each
"denied" assertion keeps the real catches in place.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew import security


@pytest.fixture
def gateway_cwd_holds_a_fence(monkeypatch) -> Path:
    """Pin the process cwd to a directory that holds the (per-test) crew data home.

    The rootdir conftest re-anchors ``KIROCREW_HOME`` per test, and the fence lists
    follow that re-anchoring, so the parent of the data home is a directory under
    which a fenced leaf sits -- the shape the desktop app's ``/`` cwd has.
    """
    home = Path(os.environ["KIROCREW_HOME"])
    holder = home.parent
    holder.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(holder)
    assert security.path_contains_sensitive(str(holder)), "fixture premise: cwd holds a fence"
    return holder


# Denied ONLY because the old code resolved the relative root against the gateway cwd.
CWD_RELATIVE_ALLOWED = [
    "grep -rn version .",
    "cd /Volumes/workplace/Foo && grep -rn version .",
    "rg secret .",
    "fd .env -x cat",
    "find . -type f -exec cat {} +",
    "find . -name '*.py' -exec grep X {} +",
    # A fenced LEAF NAME under an unknowable directory is a project-local file as
    # often as not (`.env` in a checkout); the directory decides, and it is unknown.
    "find . -name credentials -exec cat {} +",
    # A computed variable whose stage names only the shell's own directory.
    'D=$(ls .); rg secret "$D"',
]

# The same shapes with the directory made knowable: a `cd` base or an anchored root.
STILL_DENIED = [
    "cd ~ && rg secret .",
    "cd $HOME && grep -r AKIA .",
    "cd ~; fd .env -x cat",
    "cd ~; rg secret",
    "cd ~ && find . -type f -exec cat {} +",
    "cd ~ && find . -name credentials -exec cat {} +",
    "find ~ -name credentials -exec cat {} +",
    "find ~/.kiro/crew -type f -exec cat {} +",
    # A computed variable whose stage names the fence in plain text.
    'D=$(ls ~/.kiro/crew); rg secret "$D"',
    # Literal and behaviour matchers, untouched by this change.
    "cat ~/.aws/credentials",
    "echo x > ~/.kiro/crew/security_policy.json",
    "env | grep AWS_SECRET_ACCESS_KEY",
]


class TestRelativeRootsFollowTheCommandsOwnCd:
    @pytest.mark.parametrize("command", CWD_RELATIVE_ALLOWED)
    def test_a_root_relative_to_an_unknown_directory_is_allowed(
        self, command: str, gateway_cwd_holds_a_fence: Path
    ) -> None:
        assert security.is_sensitive_bash_command(command) is None

    @pytest.mark.parametrize("command", STILL_DENIED)
    def test_a_knowable_fenced_root_is_still_denied(
        self, command: str, gateway_cwd_holds_a_fence: Path
    ) -> None:
        assert security.is_sensitive_bash_command(command) is not None

    def test_the_gateway_cwd_is_never_the_base(self, gateway_cwd_holds_a_fence: Path) -> None:
        # Non-vacuous half of the fixture: the OLD resolution of `.` against the
        # process cwd does reach a fence, so the allow above is the change, not luck.
        assert security.path_contains_sensitive(".")
        assert security.is_sensitive_bash_command("grep -r AKIA .") is None

    def test_a_relative_root_joins_onto_every_cd_base(self) -> None:
        fenced = security._alt_root_reaching_fence(
            ["crew"], {}, cd_bases=[os.path.expanduser("~/.kiro")]
        )
        assert fenced is not None and fenced.endswith(os.path.join(".kiro", "crew"))

    def test_the_implicit_root_is_the_cd_base_or_nothing(self) -> None:
        assert security._alt_implicit_cwd_root([]) is None
        assert security._alt_implicit_cwd_root([os.path.expanduser("~")]) is not None
        assert security._alt_implicit_cwd_root(["/nonexistent/clean"]) is None

    def test_find_roots_anchor_onto_the_cd_base(self) -> None:
        anchored = security._find_anchor_relative_roots(["."], "cd /tmp/clean && find . -type f")
        assert anchored == [os.path.join("/tmp/clean", ".")]

    def test_find_roots_without_a_cd_stay_as_written_and_are_not_compared(self) -> None:
        # Kept, so the width and brace budgets still see them ...
        assert security._find_anchor_relative_roots(["."], "find . -type f") == ["."]
        wide = "find " + " ".join(f"d{i}" for i in range(security._FIND_ROOT_BUDGET + 5))
        assert security.is_sensitive_bash_command(wide + " -type f -exec cat {} +")
        # ... but never compared against the fence, whatever the process cwd holds.
        assert security._is_cwd_relative(".") and not security._is_cwd_relative("~/x")
        assert not security._is_cwd_relative("/etc") and not security._is_cwd_relative("$D/y")

    def test_anchored_find_roots_pass_through(self) -> None:
        roots = ["/etc", "~/x", "$D/y"]
        assert security._find_anchor_relative_roots(roots, "find /etc") == roots
