"""A multicall provider CLI keeps the name it dispatches on wherever that is safe.

One binary installed behind a symlink per command (busybox-style) picks its
command from ``argv[0]``. ``validate_provider_executable`` returns the link's
own spelling for such an install when launching by the link lets nobody
retarget the launch who could not already change the target, and every consumer
launches whatever the resolver returns, so none of them carries code of its own
for this.

Ownership is answered from a model rather than read from the host: CI runners
and sandboxed dev hosts disagree about who owns ``/`` and the temp dirs, and the
rule is a function of those answers alone. Where a test launches a multicall
binary, a shell reporting the basename of its ``$0`` stands in for one.
"""

from __future__ import annotations

import logging
import os
import shlex
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew import github_runner, platform_compat
from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client as gl
from kiro_crew.dashboard.handlers import source_providers as source

pytestmark = pytest.mark.skipif(os.name != "posix", reason="multicall links are a POSIX layout")

# bash where it exists: a `/bin/sh` that is a launcher for another shell may not
# hand the name it was called by on to that shell.
_SHELL = Path("/bin/bash") if Path("/bin/bash").exists() else Path("/bin/sh")
_needs_a_plain_shell = pytest.mark.skipif(
    _SHELL.resolve().name == "busybox",
    reason="a busybox shell dispatches on the link name itself",
)

# The command name as a multicall binary derives it: the basename of argv[0].
_REPORT_NAME = 'printf "%s\\n" "${0##*/}"'
_REPORT_NAME_JSON = 'printf \'{"tool": "%s"}\' "${0##*/}"'


@pytest.fixture(autouse=True)
def _hermetic_resolution(_floor_monkeypatch):
    for name in (
        github_runner.GH_BIN_ENV,
        github_runner.GH_PREVALIDATED_ENV,
        github_runner.STRICT_PROVIDER_BIN_ENV,
        "KIROCREW_GLAB_BIN",
        "KIROCREW_ISSUE_RADAR_GLAB",
    ):
        _floor_monkeypatch.delenv(name, raising=False)
    _floor_monkeypatch.setattr(github_runner, "_RESOLVE_CACHE", {})
    _floor_monkeypatch.setattr(github_runner, "_REPORTED_RESOLVED_LAUNCHES", set())
    _floor_monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    # The rule applies where an ACL's write shows in the mode bits. Pinning that
    # answer runs these layouts the same on every POSIX host;
    # TestWhereAnACLCanHideAWriter covers the other answer.
    _floor_monkeypatch.setattr(github_runner, "_acl_writes_show_in_mode_bits", lambda: True)


def _model_ownership(
    monkeypatch,
    *,
    user_owned: tuple[Path, ...],
    shared: tuple[Path, ...] = (),
    sticky: tuple[Path, ...] = (),
    foreign_links: tuple[Path, ...] = (),
) -> None:
    """Answer the ownership walk from a model: anything at or under a
    *user_owned* path is the gateway user's, anything at or under a *shared*
    path is root's but writable by a group the gateway user is not in, each
    *sticky* directory itself is a root-owned temp dir every account may add
    entries to, each of *foreign_links* is a symlink another account owns, and
    everything else is root's alone."""
    owned = tuple(path.resolve() for path in user_owned)
    others = tuple(path.resolve() for path in shared)
    temp_dirs = tuple(path.resolve() for path in sticky)
    foreign = tuple(link.parent.resolve() / link.name for link in foreign_links)

    def under(path: Path, roots: tuple[Path, ...]) -> bool:
        return any(path == root or root in path.parents for root in roots)

    def check(path: Path, *, label: str, uid: int, strict: bool) -> None:
        if strict and under(path, owned):
            raise ValueError(f"{label} is not root-owned")
        if strict and under(path, others):
            raise ValueError(f"{label} is writable by a group")
        if strict and path in temp_dirs:
            raise ValueError(f"{label} is writable by the gateway user")

    def who_can_change(path: Path, *, uid: int) -> tuple[bool, bool]:
        if path in temp_dirs:
            return False, True
        return under(path, owned), under(path, others)

    def symlink_owner(link: Path) -> int:
        return os.geteuid() + 1 if link in foreign else os.geteuid()

    monkeypatch.setattr(github_runner, "check_provider_path_component", check)
    monkeypatch.setattr(github_runner, "_who_can_change", who_can_change)
    monkeypatch.setattr(github_runner, "_symlink_owner", symlink_owner)


def _multicall(directory: Path, name: str = "multicall") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / name
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _link(directory: Path, name: str, target: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    link = directory / name
    link.symlink_to(target)
    return link


def _never_asked(path, gid):
    raise AssertionError("the group was asked about when the mode already settled the answer")


class TestWhichSpellingIsLaunched:
    def test_a_link_to_a_target_the_user_already_owns_keeps_its_name(self, monkeypatch, tmp_path):
        """The per-user tool-distribution layout: rewriting the target is already
        open to the user, so launching by the link opens nothing new."""
        home = tmp_path / "home"
        link = _link(home / "bin", "glab", _multicall(home / "tools"))
        _model_ownership(monkeypatch, user_owned=(home,))

        assert github_runner.validate_provider_executable(str(link)) == str(link)

    def test_a_link_only_root_can_change_keeps_its_name(self, monkeypatch, tmp_path):
        """The system package layout (snap, busybox): neither spelling moves
        without root."""
        system = tmp_path / "system"
        link = _link(system / "bin", "glab", _multicall(system / "lib"))
        _model_ownership(monkeypatch, user_owned=())

        assert github_runner.validate_provider_executable(str(link)) == str(link)

    def test_a_user_link_to_a_target_only_root_can_change_launches_the_target(
        self, monkeypatch, tmp_path
    ):
        """The one shape where the link is the only swappable piece."""
        home = tmp_path / "home"
        target = _multicall(tmp_path / "system" / "lib")
        link = _link(home / "bin", "glab", target)
        _model_ownership(monkeypatch, user_owned=(home,))

        assert github_runner.validate_provider_executable(str(link)) == str(target.resolve())

    def test_a_user_link_to_a_target_another_group_can_change_launches_the_target(
        self, monkeypatch, tmp_path
    ):
        """Failing the strict check is not the same as being the user's to change:
        with a target only another group can write, the link is still the one
        piece the user can swap."""
        home = tmp_path / "home"
        shared = tmp_path / "shared"
        target = _multicall(shared / "lib")
        link = _link(home / "bin", "glab", target)
        _model_ownership(monkeypatch, user_owned=(home,), shared=(shared,))

        assert github_runner.validate_provider_executable(str(link)) == str(target.resolve())

    def test_a_link_in_a_directory_another_group_can_write_launches_the_target(
        self, monkeypatch, tmp_path
    ):
        """The user can rewrite the target already, but the group that can write
        the link's directory cannot, and must not gain a way to retarget the
        launch."""
        home = tmp_path / "home"
        shared = tmp_path / "shared"
        target = _multicall(home / "tools")
        link = _link(shared / "bin", "glab", target)
        _model_ownership(monkeypatch, user_owned=(home,), shared=(shared,))

        assert github_runner.validate_provider_executable(str(link)) == str(target.resolve())

    def test_a_user_owned_hop_launches_the_target(self, monkeypatch, tmp_path):
        """Root owns the link's own directory, but the chain passes through a link
        the user can retarget, and retargeting it retargets the launch."""
        home = tmp_path / "home"
        system = tmp_path / "system"
        target = _multicall(system / "lib")
        hop = _link(home / "links", "glab", target)
        link = _link(system / "bin", "glab", hop)
        _model_ownership(monkeypatch, user_owned=(home,))

        assert github_runner.validate_provider_executable(str(link)) == str(target.resolve())

    def test_a_link_another_account_owns_in_a_shared_temp_dir_launches_the_target(
        self, monkeypatch, tmp_path
    ):
        """The temp dir is on both sides of the walk, and its owner-only rule
        stops the other account at the user's target but not at its own link."""
        temp = tmp_path / "tmp"
        target = _multicall(temp / "mine")
        link = _link(temp, "glab", target)
        _model_ownership(
            monkeypatch, user_owned=(temp / "mine",), sticky=(temp,), foreign_links=(link,)
        )

        assert github_runner.validate_provider_executable(str(link)) == str(target.resolve())

    def test_the_users_own_link_in_a_shared_temp_dir_keeps_its_name(self, monkeypatch, tmp_path):
        """Nobody else may replace the user's own entry in a sticky directory."""
        temp = tmp_path / "tmp"
        link = _link(temp, "glab", _multicall(temp / "mine"))
        _model_ownership(monkeypatch, user_owned=(temp / "mine",), sticky=(temp,))

        assert github_runner.validate_provider_executable(str(link)) == str(link)

    def test_a_hop_another_account_owns_launches_the_target(self, monkeypatch, tmp_path):
        """Every symlink the walk follows counts, not only the one it starts at."""
        home = tmp_path / "home"
        temp = tmp_path / "tmp"
        target = _multicall(temp / "mine")
        hop = _link(temp, "hop", target)
        link = _link(home / "bin", "glab", hop)
        _model_ownership(
            monkeypatch, user_owned=(home, temp / "mine"), sticky=(temp,), foreign_links=(hop,)
        )

        assert github_runner.validate_provider_executable(str(link)) == str(target.resolve())

    def test_a_link_inside_an_agent_writable_root_launches_the_target(self, monkeypatch, tmp_path):
        """The agent writes its own tree whatever the ownership says."""
        home = tmp_path / "home"
        target = _multicall(home / "tools")
        link = _link(home / "project" / "bin", "glab", target)
        _model_ownership(monkeypatch, user_owned=(home,))
        project = (home / "project").resolve()
        monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: (project,))

        assert github_runner.validate_provider_executable(str(link)) == str(target.resolve())

    def test_a_link_named_like_its_target_launches_the_target(self, monkeypatch, tmp_path):
        """The Homebrew layout: the target's own name already is the command."""
        home = tmp_path / "home"
        target = _multicall(home / "Cellar" / "gh" / "2.0.0" / "bin", name="gh")
        link = _link(home / "bin", "gh", target)
        _model_ownership(monkeypatch, user_owned=(home,))

        assert github_runner.validate_provider_executable(str(link)) == str(target.resolve())

    def test_strict_mode_still_refuses_the_link(self, monkeypatch, tmp_path):
        system = tmp_path / "system"
        link = _link(system / "bin", "glab", _multicall(system / "lib"))
        _model_ownership(monkeypatch, user_owned=())
        monkeypatch.setenv(github_runner.STRICT_PROVIDER_BIN_ENV, "1")

        with pytest.raises(ValueError, match="canonical"):
            github_runner.validate_provider_executable(str(link))


class TestWhoCanChange:
    """The per-component answer the rule is built from, read from the host."""

    def test_the_users_own_component_is_theirs_even_without_a_write_bit(self, tmp_path):
        """Its owner can restore the write bit at any time."""
        tool = tmp_path / "tool"
        tool.write_text("", encoding="utf-8")
        tool.chmod(0o555)

        assert github_runner._who_can_change(tool, uid=os.geteuid()) == (True, False)

    def test_a_group_write_bit_lets_someone_else_change_it(self, monkeypatch, tmp_path):
        """Unless the group provably holds the gateway user alone, its other
        members can replace what the directory holds."""
        directory = tmp_path / "bin"
        directory.mkdir()
        directory.chmod(0o775)
        monkeypatch.setattr(
            platform_compat,
            "group_write_admits_another_account",
            lambda path, gid: "group 'peers', shared with 3 other account(s)",
        )

        assert github_runner._who_can_change(directory, uid=os.geteuid()) == (True, True)

    def test_a_group_holding_the_user_alone_is_the_users_own(self, monkeypatch, tmp_path):
        """A user-private group's bit, which every directory a per-user install
        creates carries on a host with umask 002."""
        directory = tmp_path / "bin"
        directory.mkdir()
        directory.chmod(0o775)
        asked = []

        def admits(path, gid):
            asked.append((path, gid))
            return None

        monkeypatch.setattr(platform_compat, "group_write_admits_another_account", admits)

        assert github_runner._who_can_change(directory, uid=os.geteuid()) == (True, False)
        assert asked == [(directory, directory.stat().st_gid)]

    @pytest.mark.parametrize("mode", [0o755, 0o777])
    def test_a_mode_that_settles_it_never_asks_about_the_group(self, monkeypatch, tmp_path, mode):
        """Without a group bit there is nothing to ask, and a bit for everyone
        admits every account whoever holds the group."""
        directory = tmp_path / "bin"
        directory.mkdir()
        directory.chmod(mode)
        monkeypatch.setattr(platform_compat, "group_write_admits_another_account", _never_asked)

        assert github_runner._who_can_change(directory, uid=os.geteuid()) == (True, mode == 0o777)

    def test_write_access_to_a_sticky_directory_someone_else_owns_does_not_count(self):
        """The user may add entries to a shared temp dir, but cannot replace
        anyone else's there."""
        shared_tmp = Path("/tmp")
        try:
            info = shared_tmp.stat()
        except OSError:
            pytest.skip("no /tmp on this host")
        if not (
            stat.S_ISDIR(info.st_mode)
            and info.st_mode & stat.S_ISVTX
            and info.st_mode & stat.S_IWOTH
            and info.st_uid != os.geteuid()
            and os.access(shared_tmp, os.W_OK)
        ):
            pytest.skip("/tmp is not a shared sticky directory here")

        assert github_runner._who_can_change(shared_tmp, uid=os.geteuid()) == (False, True)

    def test_a_symlink_is_answered_for_its_own_owner_not_its_targets(self, tmp_path):
        """In a sticky directory a symlink's owner can replace it, whoever owns
        the file it points at."""
        if _SHELL.stat().st_uid == os.geteuid():
            pytest.skip("the shell belongs to the test user here")
        link = _link(tmp_path, "glab", _SHELL)

        assert github_runner._symlink_owner(link) == os.geteuid()

    def test_an_unreadable_symlink_is_a_refusal_not_an_answer(self, tmp_path):
        with pytest.raises(ValueError, match="not accessible"):
            github_runner._symlink_owner(tmp_path / "missing")


# The gate as the module defines it, before the autouse fixture pins it.
_ACL_WRITES_SHOW_IN_MODE_BITS = github_runner._acl_writes_show_in_mode_bits


class TestWhereAnACLCanHideAWriter:
    """Off Linux an ACL entry can grant write without touching the mode bits, and
    every answer the rule reads comes from the mode bits, so no link is kept."""

    @pytest.mark.parametrize("linux", [True, False])
    def test_only_linux_shows_every_acl_writer_in_the_mode_bits(self, monkeypatch, linux):
        monkeypatch.setattr(github_runner.platform_compat, "IS_LINUX", linux)

        assert _ACL_WRITES_SHOW_IN_MODE_BITS() is linux

    def test_a_link_the_rule_would_keep_launches_the_target_and_warns(
        self, monkeypatch, tmp_path, caplog
    ):
        home = tmp_path / "home"
        target = _multicall(home / "tools")
        link = _link(home / "bin", "glab", target)
        _model_ownership(monkeypatch, user_owned=(home,))
        monkeypatch.setattr(github_runner, "_acl_writes_show_in_mode_bits", lambda: False)

        with caplog.at_level(logging.WARNING, logger=github_runner.__name__):
            assert github_runner.validate_provider_executable(str(link)) == str(target.resolve())

        [message] = TestReportsALinkItRulesOut._warnings(caplog)
        assert github_runner._UNSEEN_ACL_REASON in message
        assert github_runner.PROVIDER_CLI_OVERRIDE_ENV["glab"] in message
        # No component is named, so none is offered as the thing to change.
        assert "change what that names" not in message


class TestReportsALinkItRulesOut:
    """A multicall CLI launched by its resolved path fails with an error of its
    own, so the resolver's warning is where the cause and the remedy are named."""

    @staticmethod
    def _warnings(caplog) -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.name == github_runner.__name__ and record.levelno == logging.WARNING
        ]

    def test_the_warning_names_the_component_and_the_override(self, monkeypatch, tmp_path, caplog):
        home = tmp_path / "home"
        shared = tmp_path / "shared"
        target = _multicall(home / "tools")
        link = _link(shared / "bin", "glab", target)
        _model_ownership(monkeypatch, user_owned=(home,), shared=(shared,))

        with caplog.at_level(logging.WARNING, logger=github_runner.__name__):
            github_runner.validate_provider_executable(str(link))

        [message] = self._warnings(caplog)
        assert f"{str(shared.resolve())!r} (uid" in message
        assert github_runner.PROVIDER_CLI_OVERRIDE_ENV["glab"] in message

    def test_a_foreign_symlink_is_named(self, monkeypatch, tmp_path, caplog):
        temp = tmp_path / "tmp"
        link = _link(temp, "glab", _multicall(temp / "mine"))
        _model_ownership(
            monkeypatch, user_owned=(temp / "mine",), sticky=(temp,), foreign_links=(link,)
        )

        with caplog.at_level(logging.WARNING, logger=github_runner.__name__):
            github_runner.validate_provider_executable(str(link))

        [message] = self._warnings(caplog)
        assert f"the symlink {str(link.parent.resolve() / link.name)!r} belongs" in message

    def test_each_link_is_reported_once(self, monkeypatch, tmp_path, caplog):
        home = tmp_path / "home"
        link = _link(home / "bin", "glab", _multicall(tmp_path / "system" / "lib"))
        _model_ownership(monkeypatch, user_owned=(home,))

        with caplog.at_level(logging.WARNING, logger=github_runner.__name__):
            for _ in range(3):
                github_runner.validate_provider_executable(str(link))

        assert len(self._warnings(caplog)) == 1

    def test_a_kept_link_and_a_same_named_one_are_not_reported(self, monkeypatch, tmp_path, caplog):
        home = tmp_path / "home"
        kept = _link(home / "bin", "glab", _multicall(home / "tools"))
        same = _link(home / "bin", "gh", _multicall(tmp_path / "system" / "bin", name="gh"))
        _model_ownership(monkeypatch, user_owned=(home,))

        with caplog.at_level(logging.WARNING, logger=github_runner.__name__):
            assert github_runner.validate_provider_executable(str(kept)) == str(kept)
            github_runner.validate_provider_executable(str(same))

        assert self._warnings(caplog) == []


class TestUserPrivateGroups:
    """On a host with user-private groups and umask 002, every directory a
    per-user install creates is group-writable to a group holding that user
    alone. The rule and the warning read these real directories; the stubbed
    cases accept the ancestors' ownership policy instead of reading it, for the
    reason the module docstring gives."""

    @staticmethod
    def _install(tmp_path) -> tuple[Path, Path]:
        tools = tmp_path / "tools"
        target = _multicall(tools / "lib")
        link = _link(tools / "bin", "glab", target)
        (tools / "bin").chmod(0o775)
        return link, target

    @staticmethod
    def _accept_the_ancestors(monkeypatch) -> None:
        monkeypatch.setattr(
            github_runner, "check_provider_path_component", lambda path, *, label, uid, strict: None
        )

    def test_the_link_keeps_its_name(self, monkeypatch, tmp_path):
        link, _ = self._install(tmp_path)
        self._accept_the_ancestors(monkeypatch)
        monkeypatch.setattr(
            platform_compat, "group_write_admits_another_account", lambda path, gid: None
        )

        assert github_runner.validate_provider_executable(str(link)) == str(link)

    def test_a_shared_group_launches_the_target_and_names_the_group(
        self, monkeypatch, tmp_path, caplog
    ):
        link, target = self._install(tmp_path)
        self._accept_the_ancestors(monkeypatch)
        monkeypatch.setattr(
            platform_compat,
            "group_write_admits_another_account",
            lambda path, gid: "group 'peers', shared with 3 other account(s)",
        )

        with caplog.at_level(logging.WARNING, logger=github_runner.__name__):
            assert github_runner.validate_provider_executable(str(link)) == str(target.resolve())

        [message] = TestReportsALinkItRulesOut._warnings(caplog)
        directory = link.parent.resolve()
        assert (
            f"{str(directory)!r} (uid {os.geteuid()}, mode 0775) can be changed by an account "
            "other than the gateway user and root: its group write bit admits group 'peers', "
            f"shared with 3 other account(s) (chmod g-w {shlex.quote(str(directory))} removes it)"
        ) in message

    def test_with_nothing_stubbed_where_this_hosts_group_is_private(self, tmp_path):
        """The real question, the real rule and the real ownership policy, on a
        host whose own databases show the directory's group holding this
        account alone."""
        link, _ = self._install(tmp_path)
        directory = link.parent
        verdict = platform_compat.group_write_admits_another_account(
            directory, directory.stat().st_gid
        )
        if verdict is not None:
            pytest.skip(f"the test directory's group is not private here: {verdict}")

        assert github_runner.validate_provider_executable(str(link)) == str(link)


@_needs_a_plain_shell
class TestConsumersLaunchTheKeptName:
    """No consumer knows about the rule: each launches the resolver's answer."""

    @pytest.fixture
    def installed_as(self, monkeypatch, tmp_path):
        """Install the stand-in behind a user-owned link, as a per-user tool
        distribution does, with the binary behind it the user's too."""

        def install(name: str) -> Path:
            home = tmp_path / "home"
            link = _link(home / "bin", name, _SHELL)
            _model_ownership(monkeypatch, user_owned=(home, _SHELL))
            return link

        return install

    def test_issue_radar_glab(self, monkeypatch, installed_as):
        monkeypatch.setenv("KIROCREW_ISSUE_RADAR_GLAB", str(installed_as("glab")))
        monkeypatch.setattr(gl, "_glab_bin_cache", None)
        monkeypatch.setattr(gl, "_audit", lambda *args, **kwargs: None)

        proc = gl._glab_run(["glab", "-c", _REPORT_NAME], host="gitlab.com", timeout=30.0)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "glab"

    def test_run_gh(self, monkeypatch, installed_as):
        monkeypatch.setenv(github_runner.GH_BIN_ENV, str(installed_as("gh")))
        monkeypatch.setattr(github_runner, "_audit_run", lambda *args, **kwargs: None)

        proc = github_runner.run_gh(
            [github_runner.resolve_gh(), "-c", _REPORT_NAME], timeout=30, audit_caller="core:test"
        )

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "gh"

    @pytest.mark.asyncio
    async def test_changes_panel_gh(self, monkeypatch, installed_as):
        monkeypatch.setenv(github_runner.PROVIDER_CLI_OVERRIDE_ENV["gh"], str(installed_as("gh")))
        monkeypatch.setattr(source, "_sel", lambda: MagicMock())
        # Every sandbox wrapper hands the command on positionally; the identity
        # stand-in keeps this independent of which backend the host has.
        monkeypatch.setattr(
            source, "sandboxed_spawn_argv", lambda argv, **kwargs: (argv, kwargs["env"], None)
        )

        assert await source._run_json("gh", "-c", _REPORT_NAME_JSON) == {"tool": "gh"}


class TestPrevalidatedHandoff:
    def test_the_handoff_carries_the_link_and_pins_what_it_points_at(self, monkeypatch, tmp_path):
        """A sandboxed child receives the kept spelling, and the identity pin
        still refuses it once the link points somewhere else."""
        home = tmp_path / "home"
        link = _link(home / "bin", "gh", _multicall(home / "tools"))
        other = _multicall(home / "tools", name="other")
        _model_ownership(monkeypatch, user_owned=(home,))
        monkeypatch.setenv(github_runner.GH_BIN_ENV, str(link))

        handoff = github_runner.prevalidated_gh_env()[github_runner.GH_PREVALIDATED_ENV]

        assert handoff.rpartition("|")[0] == str(link)
        assert github_runner._consume_prevalidated(handoff) == str(link)

        link.unlink()
        link.symlink_to(other)
        with pytest.raises(github_runner.SetupError, match="identity mismatch"):
            github_runner._consume_prevalidated(handoff)
