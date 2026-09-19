"""A restart must survive an update that PRUNES the tree it was running from.

An install shape that gives each release its own versioned directory and removes
the previous one when it installs the next leaves ``sys.executable`` naming a
deleted file. ``wheel_engine.respawn_executable`` special-cases exactly one
layout (the ``cli.sh`` managed venv, re-resolved through its stable link) and
answers the cached ``sys.executable`` for every other shape, so on such an
install the restart that FOLLOWS a successful update could never happen: the
dashboard handler refused with "invalid Python executable path" and the
orchestrator's own path had no guard at all and would have raised ENOENT out of
``os.execv`` with every session already closed.

These tests drive both real restart paths with the interpreter deleted and pin
the recovery: the pathname the process was launched through carries the exec
instead. They also pin the refusals, because the recovery must never become a
way around the interpreter guard.
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kiro_crew import gateway_restart, platform_compat
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers import updates
from kiro_crew.gateway_restart import resolve_launch_shim, resolve_restart_launcher
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import reset_context, set_context
from kiro_crew.slack import gateway as gw_module
from kiro_crew.slack.gateway import GatewayOrchestrator

#: The interpreter the update deleted. Never created on disk by these tests —
#: absence is the whole condition under test.
_PRUNED_INTERPRETER = "/opt/kirocrew/0.6.0/bin/python3"

#: The console-script basename this platform accepts, asked of the code under test
#: rather than spelled out. ``kirocrew`` on POSIX, ``kirocrew.exe`` on Windows
#: where pip generates a native launcher. A fixture hardcoding the POSIX spelling
#: is refused on its NAME there, so every assertion aimed at some later rule would
#: pass for that reason instead of the one it is testing.
_LAUNCHER = gateway_restart._console_script_name()


def _own_only(monkeypatch, mine: set[str]) -> None:
    """Attribute ownership per path, keeping every other ``stat`` field REAL.

    ``Path.is_symlink`` goes through ``os.stat(follow_symlinks=False)``, so a stub
    returning a bare object with only ``st_uid`` raises ``AttributeError`` on
    ``st_mode`` inside the code under test. That is swallowed as "cannot tell", which
    answers agent-chosen -- so an ownership REFUSAL test would pass without the
    ownership rule ever being consulted. Copying the real fields and overriding one
    keeps the stub honest. ``os.access`` is denied alongside it, so ownership is the
    only thing left that can answer.
    """
    real_stat = os.stat
    uid = os.geteuid()

    def _stat(candidate, *args, **kwargs):
        st = real_stat(candidate, *args, **kwargs)
        fields = {name: getattr(st, name) for name in dir(st) if name.startswith("st_")}
        fields["st_uid"] = uid if str(candidate) in mine else uid + 1
        return SimpleNamespace(**fields)

    monkeypatch.setattr(os, "stat", _stat)
    monkeypatch.setattr(os, "access", lambda *_a, **_k: False)


@pytest.fixture(autouse=True)
def _default_composition():
    """No edition launcher, so the core's own resolver decides the target.

    ``resolve_restart_launcher`` short-circuits every path it answers, and an
    edition that supplies one is not the install shape here: this issue is the
    POSIX in-dashboard case where nothing but the core is composed.
    """
    set_context(build_default_context(KiroCrewConfig()))
    assert resolve_restart_launcher() is None
    yield
    reset_context()


@pytest.fixture
def shim(tmp_path, monkeypatch):
    """A stable launcher shim on disk, published as the recorded launch pathname.

    Outside any versioned tree, which is what makes it survive the prune. The
    snapshot is patched rather than ``sys.argv`` because the resolver reads the
    value recorded at import — see ``test_ignores_a_later_argv_rewrite``.
    """
    path = tmp_path / "stable" / "bin" / _LAUNCHER
    path.parent.mkdir(parents=True)
    path.write_text('#!/bin/sh\nexec /opt/kirocrew/current/bin/python -m kiro_crew "$@"\n')
    platform_compat.chmod_safe(path, 0o700)
    monkeypatch.setattr(gateway_restart, "_LAUNCH_PATHNAME", str(path))
    # Stood in for, not constructed: the resolver requires a shim whose whole
    # ancestor chain belongs to another uid, and a test running as an ordinary
    # user cannot create one. This is the disclosed stub.
    monkeypatch.setattr(gateway_restart, "_agent_could_have_written", lambda _p: False)
    return str(path)


@pytest.fixture
def dashboard_state(monkeypatch):
    """Drive the real ``_restart_gateway`` without its teardown side effects."""
    monkeypatch.setattr("kiro_crew.dashboard.chat.save_all_slots_to_history", lambda _state: None)
    monkeypatch.setattr(updates, "flush_breadcrumb_writes", lambda *_a, **_k: None)
    return SimpleNamespace(
        _gateway_restart_in_progress=False,
        push_update_progress=Mock(),
        sessions=SimpleNamespace(close_all=AsyncMock()),
    )


@pytest.fixture
def orchestrator(monkeypatch):
    """The orchestrator's restart collaborators, drained clean."""
    monkeypatch.setattr("kiro_crew.slack.gateway.flush_breadcrumb_writes", lambda *_a, **_k: None)
    return SimpleNamespace(
        _pending_update_respawn=None,
        dashboard_state=None,
        sessions=None,
        _UPDATE_DRAIN_TIMEOUT_SECS=1,
        _drain_update_callback_work=AsyncMock(return_value=True),
    )


@pytest.fixture(autouse=True)
def execv(monkeypatch):
    """Record every exec instead of performing one, for EVERY test here.

    Autouse rather than opt-in because these tests drive the real restart code to
    its exec line. A test that reaches ``os.execv`` unpatched replaces the pytest
    process itself, and the run then ends with no summary at all -- indistinguishable
    from a pass to anything reading the output, which is how a mutation run reported
    a surviving mutant that had in fact killed the harness.
    """
    calls = Mock()
    monkeypatch.setattr(os, "execv", calls)
    return calls


class TestLaunchShimResolution:
    def test_accepts_an_absolute_executable_console_script(self, shim):
        assert resolve_launch_shim() == shim

    def test_does_not_resolve_symlinks(self, tmp_path, monkeypatch, shim):
        """A dispatcher can select behaviour from the basename it was invoked as.

        The sibling resolver documents the same rule; resolving the link here
        would hand ``execv`` the dispatcher's own name instead of the shim's.
        """
        link = tmp_path / "bin"
        link.mkdir()
        published = link / _LAUNCHER
        published.symlink_to(shim)
        monkeypatch.setattr(gateway_restart, "_LAUNCH_PATHNAME", str(published))
        assert resolve_launch_shim() == str(published)

    @pytest.mark.parametrize(
        "pathname",
        [
            "",
            "/opt/kirocrew/0.6.0/bin/kirocrew",  # pruned with its tree
        ],
        ids=["empty", "missing"],
    )
    def test_refuses_a_pathname_that_cannot_carry_an_exec(self, monkeypatch, pathname):
        monkeypatch.setattr(gateway_restart, "_LAUNCH_PATHNAME", pathname)
        assert resolve_launch_shim() is None

    def test_refuses_a_relative_pathname_even_when_it_resolves(self, tmp_path, monkeypatch):
        """A relative name resolves against a cwd the restart does not control.

        Written so the file EXISTS relative to the cwd: a case that fails the
        presence check anyway would pass this test without the absoluteness rule
        being what refused it.
        """
        target = tmp_path / _LAUNCHER
        target.write_text("#!/bin/sh\nexit 0\n")
        platform_compat.chmod_safe(target, 0o700)
        monkeypatch.chdir(tmp_path)
        assert os.access(_LAUNCHER, os.X_OK), "the relative name must resolve for this test"
        monkeypatch.setattr(gateway_restart, "_LAUNCH_PATHNAME", _LAUNCHER)
        # Ownership stubbed as trusted on purpose, so absoluteness is the only
        # rule left that can refuse; otherwise provenance would pass this test
        # for the wrong reason.
        monkeypatch.setattr(gateway_restart, "_agent_could_have_written", lambda _p: False)
        assert resolve_launch_shim() is None

    def test_a_nul_byte_is_refused_before_any_filesystem_call(self, monkeypatch, shim):
        """The NUL is rejected by inspection, not by a call that tolerates it.

        ``Path.is_file`` happens to swallow the ``ValueError`` an embedded NUL
        raises, while ``os.access`` raises it — so on today's ordering the guard
        looks redundant and a future reorder would make it load-bearing. Both
        filesystem calls are replaced with a detonator here, which pins that the
        refusal comes from the guard and not from that tolerance. The NUL sits in
        a DIRECTORY component so the basename still matches and the path is still
        absolute: nothing else is left to refuse it.
        """
        boom = Mock(side_effect=AssertionError("filesystem reached with a NUL in the path"))
        monkeypatch.setattr(os, "access", boom)
        monkeypatch.setattr(Path, "is_file", boom)
        monkeypatch.setattr(gateway_restart, "_LAUNCH_PATHNAME", f"/tmp\0{shim}")
        assert resolve_launch_shim() is None

    def test_refuses_a_directory(self, tmp_path, monkeypatch):
        target = tmp_path / _LAUNCHER
        target.mkdir()
        monkeypatch.setattr(gateway_restart, "_LAUNCH_PATHNAME", str(target))
        # Ownership stubbed as trusted, so being a directory is the only rule left.
        monkeypatch.setattr(gateway_restart, "_agent_could_have_written", lambda _p: False)
        assert resolve_launch_shim() is None

    def test_refuses_a_non_executable_file(self, tmp_path, monkeypatch):
        target = tmp_path / _LAUNCHER
        target.write_text("not executable")
        # Windows permissions do not express a POSIX executable bit.
        monkeypatch.setattr(os, "access", lambda *_a, **_k: False)
        monkeypatch.setattr(gateway_restart, "_LAUNCH_PATHNAME", str(target))
        monkeypatch.setattr(gateway_restart, "_agent_could_have_written", lambda _p: False)
        assert resolve_launch_shim() is None

    def test_refuses_a_name_that_is_not_this_project_entry_point(self, tmp_path, monkeypatch):
        """``argv[0]`` naming some other executable must not be re-entered.

        ``python -m kiro_crew`` records ``.../kiro_crew/__main__.py``, and a
        supervisor can record anything at all. Identity is checked positively
        rather than inferred from the permission bits a file happens to carry.
        """
        target = tmp_path / "kirocrew-vendor-wrapper"
        target.write_text("#!/bin/sh\nexit 0\n")
        platform_compat.chmod_safe(target, 0o700)
        monkeypatch.setattr(gateway_restart, "_LAUNCH_PATHNAME", str(target))
        # Ownership stubbed as trusted, so the name is the only rule left.
        monkeypatch.setattr(gateway_restart, "_agent_could_have_written", lambda _p: False)
        assert resolve_launch_shim() is None

    def test_windows_requires_the_native_exe_launcher(self, tmp_path, monkeypatch):
        """On Windows the entry point pip generates is ``kirocrew.exe``.

        Both halves, because one alone would pass whether or not the basename is
        platform-derived: the suffix-less POSIX name must be refused there, and
        the ``.exe`` must be accepted.
        """
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        # Windows permissions do not express a POSIX executable bit.
        monkeypatch.setattr(os, "access", lambda *_a, **_k: True)

        posix_name = tmp_path / "kirocrew"
        posix_name.write_text("#!/bin/sh\nexit 0\n")
        monkeypatch.setattr(gateway_restart, "_LAUNCH_PATHNAME", str(posix_name))
        assert resolve_launch_shim() is None

        native = tmp_path / "kirocrew.exe"
        native.write_bytes(b"MZ")
        monkeypatch.setattr(gateway_restart, "_LAUNCH_PATHNAME", str(native))
        monkeypatch.setattr(gateway_restart, "_agent_could_have_written", lambda _p: False)
        assert resolve_launch_shim() == str(native)

    def test_posix_refuses_the_windows_launcher_name(self, tmp_path, monkeypatch):
        """And the mirror: ``kirocrew.exe`` is not this platform's entry point."""
        native = tmp_path / "kirocrew.exe"
        native.write_text("#!/bin/sh\nexit 0\n")
        platform_compat.chmod_safe(native, 0o700)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(gateway_restart, "_LAUNCH_PATHNAME", str(native))
        monkeypatch.setattr(gateway_restart, "_agent_could_have_written", lambda _p: False)
        assert resolve_launch_shim() is None

    def test_ignores_a_later_argv_rewrite(self, monkeypatch, shim):
        """The value is the one recorded at import, not the live list.

        ``sys.argv`` is ordinary mutable process state that an agent turn or a
        library can rewrite long after boot. Reading it at restart time would
        make the restart target follow whatever wrote to it last, so the
        snapshot is the load-bearing half of this fix and is pinned here.
        """
        monkeypatch.setattr(gateway_restart, "_LAUNCH_PATHNAME", "")
        monkeypatch.setattr(sys, "argv", [shim, "gateway"])
        assert resolve_launch_shim() is None

    @pytest.mark.parametrize(
        "pathname", ["", "relative-launcher", "bad\0path"], ids=["empty", "relative", "nul"]
    )
    def test_shares_the_rule_set_with_the_edition_launcher(self, monkeypatch, pathname):
        """Whatever the edition launcher refuses, the shim refuses too.

        The two resolvers differ in their ERROR POLICY on purpose — an explicit
        edition target raises so a broken one can never silently fall back,
        while this one IS the fallback and answers ``None``. They must not
        differ in what they consider usable, and nothing but this test would go
        red if one of them were loosened alone.
        """
        provider = SimpleNamespace(restart_launcher=Mock(return_value=pathname))
        set_context(replace(build_default_context(KiroCrewConfig()), gateway_lifecycle=provider))
        with pytest.raises(ValueError, match="Cannot restart"):
            resolve_restart_launcher()
        monkeypatch.setattr(gateway_restart, "_LAUNCH_PATHNAME", pathname)
        assert resolve_launch_shim() is None


class TestLauncherOwnership:
    """An agent must not be able to choose what a restart executes.

    The launch pathname is an ordinary file. Where the gateway's own uid can write
    any component of its path, an agent with file tools can replace what sits there,
    then wait for an update that prunes the interpreter and have the gateway exec
    that code outside its sandbox. Ownership of the whole chain is the only answer
    this resolver accepts.
    """

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="POSIX mode semantics; the Windows answer is pinned by "
        "test_windows_has_no_permission_answer instead",
    )
    def test_a_read_only_mode_on_a_file_we_own_is_not_a_restriction(self, tmp_path):
        """The owner may chmod, so mode says nothing about what the owner can do.

        This is the case that makes ownership rather than ``os.access`` the
        question: with mode 0500 on both the file and its directory, their owner
        still restores write access and rewrites the file.
        """
        target = tmp_path / _LAUNCHER
        target.write_text("#!/bin/sh\nexit 0\n")
        platform_compat.chmod_safe(target, 0o500)
        platform_compat.chmod_safe(tmp_path, 0o500)
        try:
            assert not os.access(target, os.W_OK), "mode 0500 should deny the access probe"
            assert gateway_restart._agent_could_have_written(target) is True
        finally:
            platform_compat.chmod_safe(tmp_path, 0o700)

    def test_a_directory_we_own_makes_it_agent_chosen(self, tmp_path, monkeypatch):
        """A rename or symlink swap in the parent chooses the target too."""
        target = tmp_path / _LAUNCHER
        target.write_text("#!/bin/sh\nexit 0\n")
        _own_only(monkeypatch, mine={str(tmp_path)})
        assert gateway_restart._agent_could_have_written(target) is True

    @pytest.mark.parametrize("ours", ["file", "directory"])
    def test_a_symlink_target_we_own_makes_it_agent_chosen(self, tmp_path, monkeypatch, ours):
        """``Path.parents`` is LEXICAL, so a link's target chain is a separate walk.

        A root-owned link under ``/opt`` pointing into a tree this uid owns reads as
        beyond reach on its own spelling alone, while the file that actually runs is
        ours to replace. Parametrized over WHICH resolved component is ours, because
        walking the resolved file without its ancestors would pass the first case and
        fail the second, and one case alone cannot tell those apart.
        """
        trusted_dir = tmp_path / "opt"
        trusted_dir.mkdir()
        target_dir = tmp_path / "ours"
        target_dir.mkdir()
        target = target_dir / "payload"
        target.write_text("#!/bin/sh\nexit 0\n")
        link = trusted_dir / _LAUNCHER
        link.symlink_to(target)

        # Everything else answers "another uid", so ONLY the followed route can find
        # something of ours; the lexical walk is deliberately left with nothing.
        _own_only(monkeypatch, mine={str(target) if ours == "file" else str(target_dir)})
        assert gateway_restart._agent_could_have_written(link) is True

    def test_an_intermediate_symlink_hop_we_own_makes_it_agent_chosen(self, tmp_path):
        """Endpoint spellings miss the hop where retargeting actually happens.

        ``opt/bin`` links to ``srv/bin`` and ``srv/bin/kirocrew`` links to
        ``usr/lib/kirocrew``. The lexical chain holds ``opt``, the fully resolved
        chain holds ``usr``, and ``srv`` -- the directory whose ownership decides
        whether the middle link can be repointed -- is in neither. Built with real
        links so the route traced is the one the kernel would take.
        """
        srv_bin = tmp_path / "srv" / "bin"
        srv_bin.mkdir(parents=True)
        usr_lib = tmp_path / "usr" / "lib"
        usr_lib.mkdir(parents=True)
        real = usr_lib / _LAUNCHER
        real.write_text("#!/bin/sh\nexit 0\n")
        (srv_bin / _LAUNCHER).symlink_to(real)
        opt = tmp_path / "opt"
        opt.mkdir()
        (opt / "bin").symlink_to(srv_bin)
        entry = opt / "bin" / _LAUNCHER

        route = gateway_restart._objects_the_kernel_consults(entry)
        assert route is not None
        assert str(tmp_path / "srv" / "bin") in [str(x) for x in route], (
            "the middle hop is not on the traced route, so its ownership is never "
            f"asked about: {[str(x) for x in route]}"
        )

    def test_the_verdict_itself_sees_an_intermediate_hop(self, tmp_path, monkeypatch):
        """And the predicate acts on it, not just the enumerator.

        Only the middle directory is attributed to us. A verdict built from the two
        endpoint spellings answers "beyond reach" here, so this is what separates
        walking the route from collapsing it.
        """
        srv_bin = tmp_path / "srv" / "bin"
        srv_bin.mkdir(parents=True)
        usr_lib = tmp_path / "usr" / "lib"
        usr_lib.mkdir(parents=True)
        real = usr_lib / _LAUNCHER
        real.write_text("#!/bin/sh\nexit 0\n")
        (srv_bin / _LAUNCHER).symlink_to(real)
        opt = tmp_path / "opt"
        opt.mkdir()
        (opt / "bin").symlink_to(srv_bin)

        _own_only(monkeypatch, mine={str(srv_bin)})
        assert gateway_restart._agent_could_have_written(opt / "bin" / _LAUNCHER) is True

    def test_a_symlink_cycle_cannot_be_traced(self, tmp_path):
        """A route that cannot be walked is untrusted, not looped over."""
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.symlink_to(b)
        b.symlink_to(a)
        assert gateway_restart._objects_the_kernel_consults(a / _LAUNCHER) is None
        assert gateway_restart._agent_could_have_written(a / _LAUNCHER) is True

    def test_a_relative_pathname_has_no_traceable_route(self, tmp_path, monkeypatch):
        """Only an absolute path has a route from the root to trace."""
        monkeypatch.chdir(tmp_path)
        assert gateway_restart._objects_the_kernel_consults(Path(_LAUNCHER)) is None

    def test_an_ancestor_we_own_makes_it_agent_chosen(self, tmp_path, monkeypatch):
        """Ownership is asked of the whole chain, not the immediate parent only.

        A directory the gateway's uid owns three levels up can be swapped for one
        holding a different ``bin/kirocrew``, so stopping at the parent would call
        that shim beyond reach when it is not.
        """
        deep = tmp_path / "vendor" / "bin"
        deep.mkdir(parents=True)
        target = deep / _LAUNCHER
        target.write_text("#!/bin/sh\nexit 0\n")
        uid = os.geteuid()
        monkeypatch.setattr(
            os,
            "stat",
            lambda c, *_a, **_k: SimpleNamespace(
                st_uid=uid if str(c) == str(tmp_path) else uid + 1
            ),
        )
        monkeypatch.setattr(os, "access", lambda *_a, **_k: False)
        assert gateway_restart._agent_could_have_written(target) is True

    def test_an_unreadable_stat_is_not_trust(self, tmp_path, monkeypatch):
        """Not knowing is never permission to trust it."""
        monkeypatch.setattr(os, "stat", Mock(side_effect=OSError("stat failed")))
        assert gateway_restart._agent_could_have_written(tmp_path / _LAUNCHER) is True

    def test_windows_has_no_permission_answer(self, tmp_path, monkeypatch):
        """Windows answers agent-chosen, so the shim fallback never engages there.

        The POSIX answer is rigged to "another uid owns it, not writable", so the
        platform guard is the only thing that can still return True here.
        """
        target = tmp_path / _LAUNCHER
        target.write_text("#!/bin/sh\nexit 0\n")
        # os.geteuid does not exist on Windows, so it is stubbed rather than read:
        # this test must run on the platform whose answer it pins.
        _own_only(monkeypatch, mine=set())
        assert gateway_restart._agent_could_have_written(target) is False
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        assert gateway_restart._agent_could_have_written(target) is True

    def test_an_agent_writable_shim_is_refused(self, tmp_path, monkeypatch):
        """No second answer: a shim this uid could write does not carry a restart."""
        target = tmp_path / _LAUNCHER
        target.write_text("#!/bin/sh\nexit 0\n")
        platform_compat.chmod_safe(target, 0o700)
        monkeypatch.setattr(gateway_restart, "_LAUNCH_PATHNAME", str(target))
        assert gateway_restart._agent_could_have_written(target) is True
        assert resolve_launch_shim() is None


class TestShebangInterpreter:
    """Owning the shim is not enough when the shim is a SCRIPT.

    The kernel reads a script's ``#!`` line and runs the interpreter named there,
    so a root-owned shim over an interpreter the gateway's uid can write still hands
    that uid the executed bytes. These tests answer ownership PER PATH rather than
    globally, so the shim can be beyond reach while its interpreter is not -- the
    exact combination a single global stub would hide.
    """

    @staticmethod
    def _reach(monkeypatch, writable: set[str]) -> None:
        """Make ownership answer per path: only *writable* entries are ours."""
        monkeypatch.setattr(
            gateway_restart,
            "_agent_could_have_written",
            lambda candidate: str(candidate) in writable,
        )

    def _shim_with(self, tmp_path, monkeypatch, first_line: str) -> str:
        path = tmp_path / _LAUNCHER
        path.write_text(f"{first_line}\nexit 0\n")
        platform_compat.chmod_safe(path, 0o700)
        monkeypatch.setattr(gateway_restart, "_LAUNCH_PATHNAME", str(path))
        return str(path)

    def test_refuses_a_trusted_shim_over_an_agent_writable_interpreter(self, tmp_path, monkeypatch):
        interpreter = tmp_path / "python3"
        interpreter.write_text("#!/bin/sh\nexit 0\n")
        platform_compat.chmod_safe(interpreter, 0o700)
        shim = self._shim_with(tmp_path, monkeypatch, f"#!{interpreter}")
        self._reach(monkeypatch, writable={str(interpreter)})
        assert resolve_launch_shim() is None

    def test_accepts_a_trusted_shim_over_a_trusted_interpreter(self, tmp_path, monkeypatch):
        interpreter = tmp_path / "python3"
        interpreter.write_text("#!/bin/sh\nexit 0\n")
        platform_compat.chmod_safe(interpreter, 0o700)
        shim = self._shim_with(tmp_path, monkeypatch, f"#!{interpreter}")
        self._reach(monkeypatch, writable=set())
        assert resolve_launch_shim() == shim

    def test_refuses_an_env_shebang(self, tmp_path, monkeypatch):
        """``#!/usr/bin/env python3`` names a FINDER, not the program.

        Its first word is a root-owned binary, so an ownership check on that word
        passes while the program itself is chosen from ``PATH`` -- which a gateway's
        own ``PATH`` can lead with an agent-writable directory.
        """
        self._shim_with(tmp_path, monkeypatch, "#!/usr/bin/env python3")
        self._reach(monkeypatch, writable=set())
        assert resolve_launch_shim() is None

    @pytest.mark.parametrize(
        "first_line", ["#!python3", "#!../bin/python3", "#!"], ids=["bare", "relative", "empty"]
    )
    def test_refuses_an_interpreter_it_cannot_identify(self, tmp_path, monkeypatch, first_line):
        self._shim_with(tmp_path, monkeypatch, first_line)
        self._reach(monkeypatch, writable=set())
        assert resolve_launch_shim() is None

    def test_accepts_a_compiled_launcher_with_no_shebang(self, tmp_path, monkeypatch):
        """No ``#!`` means the kernel maps the file's own bytes.

        Ownership of the file already answered for those, so there is no second
        program to check -- and refusing here would reject pip's Windows launcher
        and every frozen build.
        """
        path = tmp_path / _LAUNCHER
        path.write_bytes(b"\x7fELF\x02\x01\x01\x00padding")
        platform_compat.chmod_safe(path, 0o700)
        monkeypatch.setattr(gateway_restart, "_LAUNCH_PATHNAME", str(path))
        self._reach(monkeypatch, writable=set())
        assert resolve_launch_shim() == str(path)

    def test_the_interpreter_path_is_the_one_ownership_is_asked_about(self, tmp_path, monkeypatch):
        """The shebang's interpreter, not the shim, is what gets judged.

        Resolution of a link target happens inside the ownership predicate, whose
        own test covers it; what this pins is that the interpreter reaches that
        predicate at all, so a shim trusted on its own path cannot smuggle an
        untrusted program in behind it.
        """
        interpreter = tmp_path / "python3"
        interpreter.write_text("#!/bin/sh\nexit 0\n")
        platform_compat.chmod_safe(interpreter, 0o700)
        shim = self._shim_with(tmp_path, monkeypatch, f"#!{interpreter}")
        asked: list[str] = []

        def _record(candidate):
            asked.append(str(candidate))
            return False

        monkeypatch.setattr(gateway_restart, "_agent_could_have_written", _record)
        assert resolve_launch_shim() == shim
        assert str(interpreter) in asked, asked

    def test_refuses_a_shim_it_cannot_read(self, tmp_path, monkeypatch):
        """An unreadable shim cannot be judged, so it is not trusted."""
        self._shim_with(tmp_path, monkeypatch, "#!/bin/sh")
        self._reach(monkeypatch, writable=set())
        monkeypatch.setattr("builtins.open", Mock(side_effect=OSError("unreadable")), raising=True)
        assert resolve_launch_shim() is None


class TestTheCheckHappensBeforeAnyTeardown:
    """The shim is judged once, before a single session is closed.

    Re-checking after the drain was tried and removed. ``close_all`` sets the
    session registry's closing flag and nothing in the tree ever clears it, so a
    refusal at that point leaves the gateway alive and permanently unable to serve --
    strictly worse than either alternative. It also buys nothing against an agent:
    the shim is refused up front unless its whole chain belongs to another uid, so
    this uid cannot alter it during the window.
    """

    @pytest.mark.asyncio
    async def test_a_shim_removed_mid_drain_is_still_exec_attempted(
        self, dashboard_state, shim, execv
    ):
        """No second verdict is taken after teardown begins.

        The exec then fails at the kernel, which is loud and leaves a supervisor
        something to restart, rather than a live gateway with a closed registry.
        """

        async def _remove():
            Path(shim).unlink()

        dashboard_state.sessions.close_all = AsyncMock(side_effect=_remove)

        result = await updates._restart_gateway(
            dashboard_state, resolver=lambda: _PRUNED_INTERPRETER
        )

        assert result is True
        execv.assert_called_once_with(shim, [shim, *sys.argv[1:]])
        errors = [
            call.args
            for call in dashboard_state.push_update_progress.call_args_list
            if call.args and call.args[0] == "error"
        ]
        assert not errors, errors

    @pytest.mark.asyncio
    async def test_dashboard_reports_an_exec_that_failed_after_teardown(
        self, dashboard_state, shim, execv
    ):
        """``execv`` does not return on success, so reaching here means it failed.

        Sessions are already closed at that point and only a relaunch restores
        service, so the operator gets a message saying exactly that instead of a
        bare traceback.
        """
        execv.side_effect = OSError(2, "No such file or directory")

        result = await updates._restart_gateway(
            dashboard_state, resolver=lambda: _PRUNED_INTERPRETER
        )

        assert result is False
        last = dashboard_state.push_update_progress.call_args.args
        assert last[0] == "error"
        assert "relaunch" in last[1].lower()

    @pytest.mark.asyncio
    async def test_orchestrator_keeps_its_retry_target_when_the_exec_fails(
        self, orchestrator, shim, execv
    ):
        """The resolver is cleared just before the exec, so it must come back.

        Without it ``_retry_pending_update_restart`` has nothing to retry and the
        applied update can never be finished short of a second apply.
        """
        respawn = Mock(return_value=_PRUNED_INTERPRETER)
        execv.side_effect = OSError(2, "No such file or directory")

        await GatewayOrchestrator._restart_after_update(orchestrator, respawn)

        assert orchestrator._pending_update_respawn is respawn
        assert orchestrator._update_apply_deferred is True

    def test_no_restart_path_re_reads_the_shim_after_draining(self):
        """Pinned as source shape, because the cost of regressing it is silent.

        A future re-check added after ``close_all`` would wedge the gateway in a way
        no test of the happy path can see.
        """
        for module in (updates, gw_module):
            source = inspect.getsource(module)
            assert "reexec_launch_shim" not in source, (
                f"{module.__name__} re-verifies the shim after draining; a refusal "
                "there leaves the session registry permanently closing"
            )


class TestDashboardRestart:
    @pytest.mark.asyncio
    async def test_execs_the_shim_when_the_interpreter_was_pruned(
        self, dashboard_state, shim, execv
    ):
        result = await updates._restart_gateway(
            dashboard_state, resolver=lambda: _PRUNED_INTERPRETER
        )

        assert result is True, (
            "the restart refused on an install whose update pruned the tree "
            "sys.executable lived in, so the update could never complete"
        )
        execv.assert_called_once_with(shim, [shim, *sys.argv[1:]])
        dashboard_state.sessions.close_all.assert_awaited_once()
        errors = [
            call.args
            for call in dashboard_state.push_update_progress.call_args_list
            if call.args and call.args[0] == "error"
        ]
        assert not errors, errors

    @pytest.mark.asyncio
    async def test_still_refuses_when_no_shim_survives(self, dashboard_state, monkeypatch, execv):
        """The recovery is additive: with nothing to fall back to, refuse.

        Draining sessions and then failing the exec is strictly worse than
        refusing, which is why the guard keeps its verdict rather than
        attempting a bare ``os.execv`` on a path known to be dead.
        """
        monkeypatch.setattr(gateway_restart, "_LAUNCH_PATHNAME", "")

        result = await updates._restart_gateway(
            dashboard_state, resolver=lambda: _PRUNED_INTERPRETER
        )

        assert result is False
        execv.assert_not_called()
        dashboard_state.sessions.close_all.assert_not_awaited()
        assert dashboard_state.push_update_progress.call_args.args == (
            "error",
            "Cannot restart: invalid Python executable path",
        )

    @pytest.mark.asyncio
    async def test_a_live_interpreter_is_still_exec_as_the_interpreter(
        self, dashboard_state, shim, execv
    ):
        """The healthy path is untouched: no shim, no launcher, same exec.

        Without this, a fallback that engaged whenever a shim merely EXISTS
        would silently move every restart onto the launcher — including the
        managed-venv promotion whose whole point is the stable-link interpreter.
        """
        result = await updates._restart_gateway(dashboard_state, resolver=lambda: sys.executable)

        assert result is True
        execv.assert_called_once()
        assert execv.call_args.args[0] == sys.executable


class TestOrchestratorRestart:
    @pytest.mark.asyncio
    async def test_execs_the_shim_when_the_interpreter_was_pruned(self, orchestrator, shim, execv):
        await GatewayOrchestrator._restart_after_update(orchestrator, lambda: _PRUNED_INTERPRETER)

        execv.assert_called_once_with(shim, [shim, *sys.argv[1:]])

    @pytest.mark.asyncio
    async def test_a_live_interpreter_is_still_exec_as_the_interpreter(
        self, orchestrator, shim, execv
    ):
        await GatewayOrchestrator._restart_after_update(orchestrator, lambda: sys.executable)

        execv.assert_called_once()
        assert execv.call_args.args[0] == sys.executable

    @pytest.mark.asyncio
    async def test_defers_without_draining_when_no_shim_survives(
        self, orchestrator, monkeypatch, execv
    ):
        """No target left: refuse before the fence, not after close_all.

        Walking the drain, the callback fence and ``close_all`` only to have
        ``os.execv`` raise ENOENT ends every session for a restart that cannot
        happen. The retained respawn plus the deferral flag send the coordinator
        back through the retry, so repairing the install finishes the update
        without a second apply.
        """
        monkeypatch.setattr(gateway_restart, "_LAUNCH_PATHNAME", "")
        orchestrator.sessions = SimpleNamespace(
            close_all=AsyncMock(), fence_update_restart=Mock(return_value=True)
        )

        await GatewayOrchestrator._restart_after_update(orchestrator, lambda: _PRUNED_INTERPRETER)

        execv.assert_not_called()
        orchestrator.sessions.close_all.assert_not_awaited()
        orchestrator.sessions.fence_update_restart.assert_not_called()
        orchestrator._drain_update_callback_work.assert_not_awaited()
        assert orchestrator._update_apply_deferred is True
        assert orchestrator._pending_update_respawn is not None, (
            "the respawn must be retained so _retry_pending_update_restart can "
            "finish the update once the install is repaired"
        )
