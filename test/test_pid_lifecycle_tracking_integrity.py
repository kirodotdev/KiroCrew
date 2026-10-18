"""Tests that keep a tracking-file entry true to the process it names.

The start-token identity guard against a recycled PID, the sandboxed agent's
launcher root, the cross-platform spawn grace, and the file I/O underneath:
atomic rewrites and a lock file that is never truncated.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from pid_lifecycle_helpers import (  # noqa: F401 -- fixtures are requested by name
    _POSIX_ONLY,
    _launcher_cmdline,
    _launcher_script_path,
    _launcher_shape,
    pid_file_fixture,
    session_pid_file_fixture,
)

from kiro_crew import platform_compat

# ── PID-recycle identity guard + cross-platform spawn grace ───────────
# The quit->reopen race: a stale ``<dead_gw>:<pid>`` entry whose PID has been
# recycled onto a LIVE kiro-cli must not be SIGKILL'd by the startup sweep
# (which would surface to the user as "process exited (rc=None)"). The file
# sweep must verify more than the cmdline, and the spawn-grace window must not
# be Linux-only.


class TestPidStartTokenIdentityGuard:
    def test_track_session_pid_records_start_token(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Entries carry ``gw:pid:token`` so the sweep can verify identity."""
        from kiro_crew.session_pid import _track_session_pid

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: "tok123")
        _track_session_pid(4242)
        assert session_pid_file.read_text(encoding="utf-8").strip() == f"{os.getpid()}:4242:tok123"

    def test_track_session_pid_falls_back_when_token_unavailable(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No token (Windows / ps failure) → legacy 2-field entry."""
        from kiro_crew.session_pid import _track_session_pid

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: None)
        _track_session_pid(4242)
        assert session_pid_file.read_text(encoding="utf-8").strip() == f"{os.getpid()}:4242"

    def test_track_session_pid_replaces_a_legacy_line_rather_than_duplicating(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A token-bearing track over a legacy line yields ONE line, the tokened one.

        The legacy line names a number, not a process; whether it was this same
        root before its identity was readable or a predecessor on a recycled
        number, the process holding the number NOW is the one being recorded, so
        the line carrying its identity is the one that stays. Never two lines."""
        from kiro_crew.session_pid import _track_session_pid

        session_pid_file.write_text(f"{os.getpid()}:4242\n")
        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: "tok123")
        _track_session_pid(4242)
        lines = session_pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == [f"{os.getpid()}:4242:tok123"]

    def test_untrack_session_pid_removes_token_entry(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Untrack matches the token-bearing form, not just the legacy one."""
        from kiro_crew.session_pid import _track_session_pid, _untrack_session_pid

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: "tok123")
        _track_session_pid(4242)
        _untrack_session_pid(4242)
        assert session_pid_file.read_text(encoding="utf-8").strip() == ""

    def test_recycled_pid_is_pruned_not_killed(self, session_pid_file: Path) -> None:
        """THE regression: token mismatch → prune the stale entry, never kill.

        The PID is live and its cmdline matches an agent, so every pre-existing
        guard passes; only the start-token comparison catches the recycle.
        """
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        # Dead gateway (999999) : live child PID, recorded with an OLD token.
        session_pid_file.write_text("999999:99998:oldtoken\n")
        kills: list[tuple[int, int]] = []

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            # Live process now reports a DIFFERENT token → PID was recycled.
            patch("kiro_crew.session_pid._pid_start_token", return_value="newtoken"),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                return_value=platform_compat.PID_ALIVE,
            ),
            # The owning gateway (999999) must read as DEAD or _skip_tagged
            # skips the entry and the test passes vacuously; the child is alive.
            patch(
                "kiro_crew.session_pid.platform_compat.pid_exists",
                side_effect=lambda p: p != 999999,
            ),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
            # Grace disabled so the ONLY thing that can save the process is the
            # identity check under test.
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
        ):
            cleanup_orphaned_sessions()

        assert kills == [], f"recycled PID was killed: {kills}"

    def test_unreadable_token_retains_entry(self, session_pid_file: Path) -> None:
        """Unknown identity must NOT prune: pruning would leak a live orphan.

        Every sweep keys off this file, so untracking a live process on one
        transient probe failure orphans it permanently (the fail-safe stated in
        _pid_gone_or_unmanaged: "any inconclusive result retains").
        """
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        entry = "999999:99998:recorded-token"
        session_pid_file.write_text(entry + "\n")
        kills: list[tuple[int, int]] = []

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            # Identity unreadable (probe failure) — neither match nor mismatch.
            patch("kiro_crew.session_pid._pid_start_token", return_value=None),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                return_value=platform_compat.PID_ALIVE,
            ),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_exists",
                side_effect=lambda p: p != 999999,
            ),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
        ):
            cleanup_orphaned_sessions()

        assert kills == [], "killed a process whose identity could not be verified"

    def test_session_roots_unreadable_token_retains_entry(self, session_pid_file: Path) -> None:
        """Same fail-safe in the periodic root sweep: retain, don't kill or drop."""
        from kiro_crew.session_pid import cleanup_orphaned_session_roots

        entry = "999999:99998:recorded-token"
        session_pid_file.write_text(entry + "\n")
        kills: list[tuple[int, int]] = []

        def fake_liveness(pid: int) -> str:
            return platform_compat.PID_DEAD if pid == 999999 else platform_compat.PID_ALIVE

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value=None),
            patch("kiro_crew.session_pid.platform_compat.pid_liveness", side_effect=fake_liveness),
            patch("kiro_crew.session_pid.platform_compat.get_ppid", return_value=1),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
        ):
            cleanup_orphaned_session_roots()

        assert kills == [], "killed a process whose identity could not be verified"
        # Entry retained so the next sweep can retry.
        assert entry in session_pid_file.read_text(encoding="utf-8")

    @_POSIX_ONLY
    def test_session_roots_subreaper_reparent_still_killed(self, session_pid_file: Path) -> None:
        """A recorded start token that MATCHES proves identity on its own.

        Orphans do not always reparent to init: a process placed in its own
        cgroup scope by the service manager reparents to that *user manager*,
        which is a subreaper. Treating any other PPid as "the PID was recycled"
        both spares the real orphan AND drops its tracking entry, so nothing
        ever reaps it again. The token is strictly stronger evidence of identity
        than the parent, so it must not be vetoed by the PPid heuristic.
        """
        from kiro_crew.session_pid import cleanup_orphaned_session_roots

        entry = "999999:99998:sametoken"
        session_pid_file.write_text(entry + "\n")
        kills: list[tuple[int, int]] = []

        def fake_liveness(pid: int) -> str:
            return platform_compat.PID_DEAD if pid == 999999 else platform_compat.PID_ALIVE

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value="sametoken"),
            patch("kiro_crew.session_pid.platform_compat.pid_liveness", side_effect=fake_liveness),
            # The subreaper that adopted the orphan -- neither init(1), nor the
            # dead gateway PID, nor the -1 probe-failure sentinel.
            patch("kiro_crew.session_pid.platform_compat.get_ppid", return_value=7447),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
        ):
            cleanup_orphaned_session_roots()

        assert (
            99998,
            platform_compat.SIGKILL,
        ) in kills, "a token-verified orphan adopted by a subreaper was not reaped"
        # And it must not be silently untracked, which is what leaks it forever.
        assert entry not in session_pid_file.read_text(encoding="utf-8")

    @_POSIX_ONLY
    def test_matching_token_still_killed(self, session_pid_file: Path) -> None:
        """A genuine orphan (token matches) is still reaped — no regression."""
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        session_pid_file.write_text("999999:99998:sametoken\n")
        kills: list[tuple[int, int]] = []

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value="sametoken"),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                return_value=platform_compat.PID_ALIVE,
            ),
            # The owning gateway (999999) must read as DEAD or _skip_tagged
            # skips the entry and the test passes vacuously; the child is alive.
            patch(
                "kiro_crew.session_pid.platform_compat.pid_exists",
                side_effect=lambda p: p != 999999,
            ),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
        ):
            cleanup_orphaned_sessions()

        assert (99998, platform_compat.SIGKILL) in kills

    @_POSIX_ONLY
    def test_legacy_entry_without_token_still_swept(self, session_pid_file: Path) -> None:
        """Back-compat: a 2-field entry keeps its old cmdline+grace behavior."""
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        session_pid_file.write_text("999999:99998\n")
        kills: list[tuple[int, int]] = []

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                return_value=platform_compat.PID_ALIVE,
            ),
            # The owning gateway (999999) must read as DEAD or _skip_tagged
            # skips the entry and the test passes vacuously; the child is alive.
            patch(
                "kiro_crew.session_pid.platform_compat.pid_exists",
                side_effect=lambda p: p != 999999,
            ),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
        ):
            cleanup_orphaned_sessions()

        assert (99998, platform_compat.SIGKILL) in kills

    @_POSIX_ONLY
    def test_session_roots_sweep_parses_token_entry(self, session_pid_file: Path) -> None:
        """cleanup_orphaned_session_roots must not mis-prune 3-field entries.

        A ``split(":", 1)`` parse would int("99998:tok") -> ValueError and prune
        the entry, silently dropping every token-bearing line from the sweep.
        """
        from kiro_crew.session_pid import cleanup_orphaned_session_roots

        session_pid_file.write_text("999999:99998:sametoken\n")
        kills: list[tuple[int, int]] = []

        def fake_liveness(pid: int) -> str:
            # Owning gateway dead; child alive.
            return platform_compat.PID_DEAD if pid == 999999 else platform_compat.PID_ALIVE

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value="sametoken"),
            patch("kiro_crew.session_pid.platform_compat.pid_liveness", side_effect=fake_liveness),
            patch("kiro_crew.session_pid.platform_compat.get_ppid", return_value=1),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
        ):
            cleanup_orphaned_session_roots()

        assert (99998, platform_compat.SIGKILL) in kills

    def test_session_roots_sweep_spares_recycled_pid(self, session_pid_file: Path) -> None:
        """Token mismatch in the periodic root sweep → prune, never kill."""
        from kiro_crew.session_pid import cleanup_orphaned_session_roots

        session_pid_file.write_text("999999:99998:oldtoken\n")
        kills: list[tuple[int, int]] = []

        def fake_liveness(pid: int) -> str:
            return platform_compat.PID_DEAD if pid == 999999 else platform_compat.PID_ALIVE

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value="newtoken"),
            patch("kiro_crew.session_pid.platform_compat.pid_liveness", side_effect=fake_liveness),
            patch("kiro_crew.session_pid.platform_compat.get_ppid", return_value=1),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
        ):
            cleanup_orphaned_session_roots()

        assert kills == [], f"recycled PID was killed: {kills}"


class TestSandboxedAgentRootIsRecognised:
    """The tracked root of a sandboxed agent is the LAUNCHER, not the harness.

    On Linux with the namespace backend -- ``detect_backend() == "namespace"``, the
    default on most distributions -- ``sandbox.namespace_argv`` wraps the spawn as
    ``<interpreter> -I -S <run dir>/kirocrew_sandbox_*.py <harness argv…>`` and the
    launcher's PARENT never execs: it writes the child's uid/gid maps and then blocks
    in ``waitpid`` for the life of the session. That parent is the pid the gateway
    records, so the gate's ``argv[0]`` was the interpreter and every sandboxed agent
    root answered "not managed".

    An unrecognised agent root is UNRECLAIMABLE, not spared: ``_sweep_pid_entries``
    RETAINS a settled-token entry the argv gate does not recognise (dropping the record
    would leave the process unfindable by every sweep) and the scope reaper refuses a
    scope holding a tracked pid, so nothing reaches the kiro-cli runtime under it until
    the gateway restarts.

    Recognition is positive and names that one shape only; the inner positional rules
    are unchanged, so nothing about which token may name a harness is relaxed to pay
    for stepping over the wrapper. Full contract:
    ``docs/system-specs/modules/session.md`` §Reclaim identity.
    """

    @pytest.mark.parametrize(
        "wrapped",
        [
            ("/usr/local/bin/kiro-cli", "acp"),
            ("/opt/kiro/bin/kiro-cli-chat", "acp", "--agent", "x"),
            ("/usr/bin/node", "/opt/n/bin/codex-acp"),
            (
                "/usr/bin/node",
                "/opt/n/lib/node_modules/@agentclientprotocol/codex-acp/dist/index.js",
            ),
        ],
    )
    def test_a_launcher_wrapped_harness_is_managed(self, wrapped: tuple[str, ...]) -> None:
        """Both inner slots answer: argv0, and a Node interpreter's script slot.

        The Node cases matter as much as the direct one -- a bespoke adapter inside the
        sandbox must be recognised the same way an unsandboxed one is, under BOTH
        spellings the resolver produces (the bin shim, whose basename is the name, and
        the package entry, matched against the published relative path).
        """
        from kiro_crew.session_pid import _cmdline_names_a_harness

        assert _cmdline_names_a_harness(_launcher_cmdline(*wrapped)) is True

    @pytest.mark.parametrize(
        "wrapped",
        [
            # Something the gateway wraps that is not an agent runtime at all.
            ("/usr/bin/make", "-j4"),
            # The harness name as an inner ARGUMENT, which says nothing about what the
            # inner process IS -- exactly the rule the unwrapped gate already applies.
            ("/usr/bin/vim", "/home/u/notes/kiro-cli"),
            ("/usr/bin/node", "build.js", "--agent", "goose"),
        ],
    )
    def test_a_launcher_wrapped_non_harness_is_not_managed(self, wrapped: tuple[str, ...]) -> None:
        """Stepping over the wrapper does not widen what the inner argv may claim.

        The launcher is Crew's, so the process under it is one Crew started -- but this
        gate authorizes a SIGKILL of a whole tree, and the sandbox wraps MCP probes and
        app backends too. Only the inner positions that would have named a harness
        unwrapped may name one here.
        """
        from kiro_crew.session_pid import _cmdline_names_a_harness

        assert _cmdline_names_a_harness(_launcher_cmdline(*wrapped)) is False

    def test_an_arbitrary_python_process_is_never_managed(self) -> None:
        """``python foo.py kiro-cli`` is not a launcher, whatever it mentions.

        The whole risk of reading past argv0 on a kill path is that a python process is
        the most ordinary thing on a host. Nothing about this one is Crew's: no launcher
        flags, and a script that is neither in Crew's run directory nor named the way
        ``sandbox.py`` names the launcher it writes.
        """
        from kiro_crew.session_pid import _cmdline_names_a_harness

        assert _cmdline_names_a_harness(b"/usr/bin/python3\x00foo.py\x00kiro-cli\x00acp") is False

    @staticmethod
    def _near_misses() -> "dict[str, tuple[str, ...]]":
        """One argv per BROKEN condition, every other condition intact.

        Built off the live shape so each case isolates exactly the condition it names --
        a hand-written path would fail several at once and stop proving that any single
        one of them is load-bearing.
        """
        from kiro_crew.sandbox import _SEATBELT_PROFILE_SUFFIX

        flags, run_dir, prefix, suffix = _launcher_shape()
        script = _launcher_script_path()
        name = os.path.basename(script)
        return {
            "no flags": ("/usr/bin/python3", script, "kiro-cli"),
            "flags reversed": ("/usr/bin/python3", *reversed(flags), script, "kiro-cli"),
            "not the interpreter": ("/usr/bin/perl", *flags, script, "kiro-cli"),
            "script outside the run dir": (
                "/usr/bin/python3",
                *flags,
                os.path.join("/opt/elsewhere", name),
                "kiro-cli",
            ),
            # The tmpdir _ensure_run_dir degrades to is NOT an accepted launcher
            # directory: it is shared with every other user of the host, so a path
            # anyone can write must not decide which trees are reclaimable.
            "the shared tmpdir": (
                "/usr/bin/python3",
                *flags,
                os.path.join(tempfile.gettempdir(), name),
                "kiro-cli",
            ),
            "not the generated prefix": (
                "/usr/bin/python3",
                *flags,
                os.path.join(run_dir, f"payload{suffix}"),
                "kiro-cli",
            ),
            "the seatbelt profile, not the launcher": (
                "/usr/bin/python3",
                *flags,
                os.path.join(run_dir, f"{prefix}1_a{_SEATBELT_PROFILE_SUFFIX}"),
                "kiro-cli",
            ),
            "nothing wrapped": ("/usr/bin/python3", *flags, script),
        }

    def test_a_near_miss_of_the_launcher_shape_opens_no_slot(self) -> None:
        """Every condition is load-bearing on its own.

        Dropping any one of them turns "is this Crew's launcher" into "does this command
        line resemble it", and the answer authorizes a SIGKILL of a process tree. A miss
        here costs a retained tracking entry re-examined next pass, which is the
        direction this module fails in everywhere. Asserted as one test over a named map
        so a failure says WHICH condition stopped holding.
        """
        from kiro_crew.session_pid import _cmdline_names_a_harness

        admitted = [
            broken
            for broken, argv in self._near_misses().items()
            if _cmdline_names_a_harness(b"\x00".join(t.encode() for t in argv))
        ]
        assert not admitted, (
            "these command lines are NOT Crew's sandbox launcher yet opened its wrapped "
            f"slot, which authorizes a SIGKILL of a tree Crew never spawned: {admitted}"
        )

    def test_a_default_home_under_a_dot_dot_HOME_still_matches(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The writer collapses the run directory, so the token comparison can be exact.

        ``config_dir()`` returns ``<HOME>/.kiro/crew`` verbatim for the DEFAULT home, so a
        ``HOME`` containing ``..`` yields ``<...>/x/../.kiro/crew/run`` -- which
        ``mkstemp`` then collapses, putting a different spelling in ``/proc`` from the one
        a raw ``config_dir() / "run"`` would produce. ``namespace_launcher_script_dir``
        normalizes it instead, so both sides agree.

        The DEFAULT home specifically: an explicit ``KIROCREW_HOME`` is ``.resolve()``d by
        ``config.paths._valid_override_home``, so it can never present this spelling and a
        test driven through it would pass with the normalization removed.
        """
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew.sandbox import namespace_launcher_script_dir
        from kiro_crew.session_pid import _cmdline_names_a_harness

        home = tmp_path / "x" / ".." / ".kiro" / "crew"
        # ``sandbox`` binds ``config_dir`` at import, so the repoint goes on that module.
        monkeypatch.setattr(sandbox_mod, "config_dir", lambda: home)
        collapsed = tmp_path / ".kiro" / "crew" / "run"
        assert str(home / "run") != str(collapsed), "the fixture no longer carries a '..'"
        assert namespace_launcher_script_dir() == str(collapsed)

        flags, _run_dir, prefix, suffix = _launcher_shape()
        # The spelling ``mkstemp`` produces for this home.
        script = os.path.join(str(collapsed), f"{prefix}1_a{suffix}")
        argv = ("/usr/bin/python3", *flags, script, "kiro-cli", "acp")

        assert _cmdline_names_a_harness(b"\x00".join(t.encode() for t in argv)) is True

    def test_a_relative_or_oddly_split_script_token_is_refused(self) -> None:
        r"""The token is compared as the writer spelled it: absolute, and ``/``-split.

        ``mkstemp`` produces an absolute, collapsed path, so anything else is not ours.
        Two spellings in particular must not match, and neither would if the comparison
        normalized the token or split its two halves differently:

        * a RELATIVE path, which ``abspath`` would resolve against the SWEEPING gateway's
          cwd -- a path the target process never named;
        * a name whose ``\`` segment makes ``os.path.dirname`` and a backslash-aware
          basename disagree about where the file name starts.
        """
        from kiro_crew.session_pid import _cmdline_names_a_harness

        flags, run_dir, prefix, suffix = _launcher_shape()
        name = f"{prefix}1_a{suffix}"
        for script in (
            os.path.join("run", name),
            os.path.join(run_dir, "..", os.path.basename(run_dir), name),
            os.path.join(run_dir, "") + name.replace(prefix, f"zz\\{prefix}"),
        ):
            argv = ("/usr/bin/python3", *flags, script, "kiro-cli")
            cmdline = b"\x00".join(token.encode() for token in argv)
            assert _cmdline_names_a_harness(cmdline) is False, script

    def test_an_unreadable_shape_declines_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate must never raise: one bad home would stop the whole sweep.

        ``namespace_launcher_script_dir`` goes through ``config_dir()``, which creates the
        data home and can fail. ``cleanup_orphaned_session_roots``'s caller swallows an
        exception with no per-entry guard, so a propagated error silently stops reclaiming
        EVERY remaining entry. Declining costs one missed reclaim instead.
        """
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew.session_pid import _cmdline_names_a_harness

        # Built BEFORE the break: the fixture resolves the same directory the gate does.
        cmdline = _launcher_cmdline("kiro-cli", "acp")

        def _boom() -> str:
            raise OSError("read-only file system")

        monkeypatch.setattr(sandbox_mod, "namespace_launcher_script_dir", _boom)

        assert _cmdline_names_a_harness(cmdline) is False
        # An ordinary harness argv is unaffected: it never reaches the shape lookup.
        assert _cmdline_names_a_harness(b"kiro-cli\x00acp") is True

    def test_an_ordinary_harness_argv_never_resolves_the_run_directory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cost: the filesystem-touching part of the shape runs LAST, or not at all.

        This gate is called once per tracked entry by every sweep, and the common case is
        an unwrapped harness. Resolving the launcher directory there would mean a
        ``config_dir()`` call -- which creates the data home -- per entry per pass, for an
        answer the free tests already have.
        """
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew.session_pid import _cmdline_names_a_harness

        def _unexpected() -> str:
            raise AssertionError("the gate resolved the run directory for a plain argv")

        monkeypatch.setattr(sandbox_mod, "namespace_launcher_script_dir", _unexpected)

        assert _cmdline_names_a_harness(b"kiro-cli\x00acp") is True
        assert _cmdline_names_a_harness(b"/usr/bin/node\x00/opt/n/bin/codex-acp") is True
        assert _cmdline_names_a_harness(b"/usr/bin/vim\x00/home/u/dsh") is False

    def test_a_launcher_inside_a_launcher_is_rejected(self) -> None:
        """Exactly ONE unwrap, pinned.

        A launcher inside a launcher is impossible on the host -- the outer launcher's
        seccomp filter denies ``unshare`` for its whole tree -- so accepting the shape
        would be authority granted for one nothing can produce. A loop here would also
        let a crafted argv bury the harness name arbitrarily deep.
        """
        from kiro_crew.session_pid import _cmdline_names_a_harness

        flags, _run_dir, _prefix, _suffix = _launcher_shape()
        script = _launcher_script_path()
        nested = (
            "/usr/bin/python3",
            *flags,
            script,
            "/usr/bin/python3",
            *flags,
            script,
            "kiro-cli",
        )

        assert _cmdline_names_a_harness(b"\x00".join(t.encode() for t in nested)) is False

    def test_the_report_only_predicate_leaves_the_launcher_to_the_mcp_sweep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_is_untracked_managed_agent_orphan`` stays argv0-only, and that is correct.

        It answers "is this a runtime NO REAPER CAN REACH", not "is this a runtime". An
        orphaned launcher IS reached: its command line carries the sandbox artifact
        prefix, so ``_is_orphan_mcp`` accepts it and the orphan-MCP sweep kills it on the
        same pass. Teaching this predicate the launcher shape would make the report log
        "leaked agent runtime, not terminated" about a pid that is already a kill
        candidate, and inflate ``leaked_untracked`` with it.

        So the asymmetry against :func:`_is_managed_agent_process` is deliberate, and this
        asserts both sides of it, because the two predicates reading the same command line
        and answering differently is the kind of thing a later change quietly "fixes".
        """
        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp, "_env_has_kirocrew_marker", lambda pid, **kw: True)
        cmdline = _launcher_cmdline("/usr/local/bin/kiro-cli", "acp")

        # The kill gate DOES recognise it; this predicate deliberately does not.
        assert sp._cmdline_names_a_harness(cmdline) is True
        assert sp._is_untracked_managed_agent_orphan(4242, cmdline, set()) is False
        assert sp._is_orphan_mcp(cmdline) is True, (
            "the orphan-MCP sweep no longer reaches a launcher, so leaving it out of the "
            "untracked-runtime report makes it invisible to everything"
        )
        # An UNWRAPPED harness orphan is still reported, which is what the predicate is for.
        assert sp._is_untracked_managed_agent_orphan(4242, b"kiro-cli\x00acp", set()) is True

    def test_the_mcp_marker_tracks_the_sandbox_artifact_prefix(self) -> None:
        """``_SANDBOX_LAUNCHER_MARKER`` duplicates the sandbox constant deliberately.

        The orphan-MCP sweep's marker tuple is a module constant, so reading it through
        the accessor would put a ``sandbox`` import back at ``session_pid`` import time --
        the one thing the lazy lookup exists to avoid. The copy is pinned here instead, the
        same way ``_BROWSER_SESSION_ENV`` is.
        """
        from kiro_crew.sandbox import _SANDBOX_ARTIFACT_PREFIX
        from kiro_crew.session_pid import _MCP_ENTRYPOINT_MARKERS, _SANDBOX_LAUNCHER_MARKER

        assert _SANDBOX_LAUNCHER_MARKER == _SANDBOX_ARTIFACT_PREFIX.encode()
        assert _SANDBOX_LAUNCHER_MARKER in _MCP_ENTRYPOINT_MARKERS

    @_POSIX_ONLY
    @patch("kiro_crew.sandbox._resolve_agent_executable", return_value="/usr/local/bin/kiro-cli")
    def test_the_argv_the_wrap_really_builds_is_recognised(self, _resolve: Mock) -> None:
        """End-to-end against ``namespace_argv`` itself, not a hand-written shape.

        This is the pin that matters: the builder and the recogniser read the same shape
        off the ``sandbox`` module's own constants, and this proves the agreement
        on a real result rather than on the accessor. A flag added to the launcher, a
        moved run directory or a renamed artifact fails here rather than silently costing
        every sandboxed root its reclaim.
        """
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew.sandbox import namespace_argv
        from kiro_crew.session_pid import _cmdline_names_a_harness

        argv = namespace_argv(["kiro-cli", "acp"], "strict")
        try:
            cmdline = b"\x00".join(token.encode() for token in argv)
            assert _cmdline_names_a_harness(cmdline) is True
        finally:
            # The module's own accessor, not index 3: that index is the flag COUNT plus
            # one, so a hardcoded 3 unlinks a flag token and leaks the tempfile as soon as
            # a flag is added -- the same reason ``_launcher_script_of`` exists.
            os.unlink(sandbox_mod._launcher_script_of(argv))

    def test_the_shape_is_read_from_the_sandbox_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No copied literal: repointing the launcher's flags moves the script slot.

        The gate reads the sandbox module's globals through the accessor at call time, so
        a launcher carrying one more flag is recognised without an edit here, and the
        two-flag spelling stops being recognised. Both halves are asserted: either one
        alone also passes against a copy of the value kept in this module.
        """
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew.session_pid import _cmdline_names_a_harness

        stock = sandbox_mod._LAUNCHER_INTERPRETER_FLAGS
        script = _launcher_script_path()
        stale = b"\x00".join(t.encode() for t in ("/usr/bin/python3", *stock, script, "kiro-cli"))
        monkeypatch.setattr(sandbox_mod, "_LAUNCHER_INTERPRETER_FLAGS", (*stock, "-X", "y"))
        grown = b"\x00".join(
            t.encode() for t in ("/usr/bin/python3", *stock, "-X", "y", script, "kiro-cli")
        )

        assert _cmdline_names_a_harness(grown) is True
        assert _cmdline_names_a_harness(stale) is False

    def test_the_artifact_prefix_is_read_from_the_sandbox_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same for the generated name: renaming the artifact moves the match.

        ``_SANDBOX_ARTIFACT_PREFIX`` is what the run-dir sweep reclaims this family by
        and what ``namespace_argv``'s ``mkstemp`` writes, so a recogniser holding its own
        copy goes quiet on a rename while every other test stays green.
        """
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew.session_pid import _cmdline_names_a_harness

        monkeypatch.setattr(sandbox_mod, "_SANDBOX_ARTIFACT_PREFIX", "kirocrew_boxed_")
        flags = sandbox_mod._LAUNCHER_INTERPRETER_FLAGS
        run_dir = sandbox_mod.namespace_launcher_script_dir()

        def _line(name: str) -> bytes:
            argv = ("/usr/bin/python3", *flags, os.path.join(run_dir, name), "kiro-cli")
            return b"\x00".join(token.encode() for token in argv)

        assert _cmdline_names_a_harness(_line("kirocrew_boxed_1_a.py")) is True
        assert _cmdline_names_a_harness(_line("kirocrew_sandbox_1_a.py")) is False


class TestSpawnGraceCrossPlatform:
    @_POSIX_ONLY
    def test_grace_applies_on_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression: the grace window was Linux-only, so macOS never got it."""
        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(sp, "_pid_age_seconds", lambda p: 5.0)
        assert sp._pid_in_spawn_grace(4242) is True

    def test_old_process_not_in_grace_on_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(sp, "_pid_age_seconds", lambda p: sp.SWEEP_SPAWN_GRACE_SECONDS + 1)
        assert sp._pid_in_spawn_grace(4242) is False

    @_POSIX_ONLY
    def test_unknown_age_treated_as_young(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unreadable age → safe direction (skip the kill)."""
        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(sp, "_pid_age_seconds", lambda p: None)
        assert sp._pid_in_spawn_grace(4242) is True

    def test_macos_age_derived_from_start_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """macOS age comes from the in-process start id — no subprocess/ps."""
        import time as _time

        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(sp.platform_compat, "IS_WINDOWS", False)
        start = _time.time() - 90.0
        monkeypatch.setattr(sp.platform_compat, "get_process_start_id", lambda p: f"{start:.6f}")
        age = sp._pid_age_seconds(4242)
        assert age is not None and 85.0 <= age <= 95.0

    def test_macos_age_none_when_identity_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(sp.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(sp.platform_compat, "get_process_start_id", lambda p: None)
        assert sp._pid_age_seconds(4242) is None

    def test_windows_young_pid_in_grace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Windows age comes from the creation FILETIME, so a young PID gets the grace."""
        import time as _time

        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.platform_compat, "IS_WINDOWS", True)
        start = _time.time() - 30.0
        filetime = int(
            (start + sp._FILETIME_UNIX_EPOCH_OFFSET_SECONDS) * sp._FILETIME_TICKS_PER_SECOND
        )
        monkeypatch.setattr(sp.platform_compat, "get_process_start_id", lambda p: str(filetime))
        assert sp._pid_in_spawn_grace(4242) is True


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: relies on fork/exec + ps for identity"
)
class TestSweepSparesLiveProcess:
    """End-to-end repro with a REAL process that the sweep spares a live kiro-cli.

    The mock-based tests above pin the decision logic; this one proves the
    whole sweep leaves an actually-running process alive. The victim is a
    short-lived ``sleep`` renamed via ``_is_managed_agent_process`` patching,
    so no kiro-cli is required and nothing user-owned is at risk.
    """

    def test_live_process_with_recycled_entry_survives(
        self, session_pid_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        monkeypatch.setattr("kiro_crew.session_pid.config_dir", lambda: tmp_path)
        victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            # Stale entry from a DEAD gateway naming the live PID, with a token
            # that cannot match the live process (the recycle signature).
            session_pid_file.write_text(f"999999:{victim.pid}:stale-token-does-not-match\n")

            with (
                # Cmdline check passes (as it did in the real incident).
                patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
                # The owning gateway (999999) must read as DEAD, or `_skip_tagged`
                # keeps the entry and the prune assertion below fails. Pinned rather
                # than assumed: 999999 is a perfectly ordinary live PID on a host
                # whose counter has passed it (`pid_max` is 4194304 here), which made
                # this a load-dependent flake rather than a constant failure. Only
                # that one PID is faked -- every other, the live victim included,
                # still goes to the real probe.
                patch(
                    "kiro_crew.session_pid.platform_compat.pid_exists",
                    side_effect=lambda p: p != 999999 and platform_compat.pid_exists(p),
                ),
                # Grace disabled: isolate the identity check as the sole guard.
                patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
                patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
            ):
                cleanup_orphaned_sessions()

            assert victim.poll() is None, "sweep SIGKILLed a live process (the bug)"
            # And the stale entry is pruned so it can't re-trigger next boot.
            assert str(victim.pid) not in session_pid_file.read_text(encoding="utf-8")
        finally:
            victim.kill()
            victim.wait()


class TestPidFileRewriteIsAtomic:
    """A PID-file rewrite must be atomic AND must never propagate its failure.

    Atomic: ``Path.write_text`` truncates the target to zero BEFORE writing the
    kept entries. A failure — or a hard kill — inside that window leaves a SHORT
    file whose surviving content is still perfectly well-formed: nothing raised,
    nothing logged, and every dropped entry is an agent runtime that no reaper
    can ever find again, because these PID files are the ONLY record of which
    runtimes this gateway owns.

    Reported, not propagated: pruning an entry is idempotent and self-retrying,
    so a failed rewrite costs one stale line. Propagating would cost the whole
    gateway — ``cleanup_orphaned_sessions`` runs unguarded on the startup path,
    and on Windows ``replace_with_retry`` declines to retry a sharing violation
    while an event loop is running.

    These tests fail the rename and then assert the original file is untouched,
    which a truncating writer cannot satisfy because by then it has already
    destroyed the original.
    """

    @staticmethod
    def _fail_rename(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    @staticmethod
    def _assert_reported(caplog: pytest.LogCaptureFixture) -> None:
        assert any(
            r.levelno >= logging.ERROR and "Could not rewrite PID file" in r.getMessage()
            for r in caplog.records
        ), "a failed PID-file rewrite must be reported at ERROR, never silently"

    def test_write_back_failure_preserves_every_entry(
        self,
        session_pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from kiro_crew.session_pid import _write_back_pid_file

        original = "100:200:tokA\n101:201:tokB\n102:202:tokC\n"
        session_pid_file.write_text(original, encoding="utf-8")
        monkeypatch.setattr("kiro_crew.atomic_write.replace_with_retry", self._fail_rename)

        with caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"):
            _write_back_pid_file({"101:201:tokB"})

        # The rewrite never landed, so the ledger must still name all three
        # runtimes. A truncating writer leaves only two — and the two it leaves
        # look entirely valid, which is what makes the loss silent.
        assert session_pid_file.read_text(encoding="utf-8") == original
        self._assert_reported(caplog)

    def test_untrack_session_pid_failure_preserves_every_entry(
        self,
        session_pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from kiro_crew.session_pid import _untrack_session_pid

        gw = os.getpid()
        original = f"{gw}:900:tokX\n{gw}:901:tokY\n"
        session_pid_file.write_text(original, encoding="utf-8")
        monkeypatch.setattr("kiro_crew.atomic_write.replace_with_retry", self._fail_rename)

        with caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"):
            _untrack_session_pid(900)

        assert session_pid_file.read_text(encoding="utf-8") == original
        self._assert_reported(caplog)

    def test_untrack_pid_failure_preserves_every_entry(
        self,
        pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from kiro_crew.session_pid import _untrack_pid

        original = "700\n701\n702\n"
        pid_file.write_text(original, encoding="utf-8")
        monkeypatch.setattr("kiro_crew.atomic_write.replace_with_retry", self._fail_rename)

        with caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"):
            _untrack_pid(701)

        assert pid_file.read_text(encoding="utf-8") == original
        self._assert_reported(caplog)

    def test_untrack_child_pids_failure_preserves_every_entry(
        self,
        pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from kiro_crew.session_pid import _untrack_child_pids

        original = "800:1\n801:1\n"
        pid_file.write_text(original, encoding="utf-8")
        monkeypatch.setattr("kiro_crew.atomic_write.replace_with_retry", self._fail_rename)

        with caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"):
            _untrack_child_pids({801: object()})

        assert pid_file.read_text(encoding="utf-8") == original
        self._assert_reported(caplog)

    @pytest.mark.asyncio
    async def test_windows_loop_sharing_violation_does_not_abort_caller(
        self,
        session_pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The startup path survives a Windows sharing violation on the rename.

        ``replace_with_retry`` deliberately refuses to sleep-retry while an event
        loop is running, so on Windows a scanner holding the temp file surfaces
        as an immediate ``PermissionError``. ``cleanup_orphaned_sessions`` calls
        this rewrite unguarded during gateway start, so the error escaping here
        would abort startup.
        """
        from kiro_crew.session_pid import _write_back_pid_file

        def _sharing_violation(*_a: object, **_kw: object) -> None:
            raise PermissionError(32, "The process cannot access the file")

        original = "100:200:tokA\n101:201:tokB\n"
        session_pid_file.write_text(original, encoding="utf-8")
        monkeypatch.setattr("kiro_crew.platform_compat.IS_WINDOWS", True)
        monkeypatch.setattr("kiro_crew.atomic_write.os.replace", _sharing_violation)

        # Runs with a live event loop, which is what disables the retry.
        with caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"):
            _write_back_pid_file({"101:201:tokB"})

        assert session_pid_file.read_text(encoding="utf-8") == original
        self._assert_reported(caplog)

    def test_successful_rewrite_lands_and_leaves_no_temp_residue(
        self, session_pid_file: Path
    ) -> None:
        from kiro_crew.session_pid import _write_back_pid_file

        session_pid_file.write_text("100:200:tokA\n101:201:tokB\n", encoding="utf-8")

        _write_back_pid_file({"101:201:tokB"})

        assert session_pid_file.read_text(encoding="utf-8") == "100:200:tokA\n"
        # atomic_write's mkstemp companion must not survive the rename.
        assert not list(session_pid_file.parent.glob("*.tmp"))

    def test_no_truncating_writer_remains_in_session_pid(self) -> None:
        """Ratchet: every PID-file rewrite goes through the atomic chokepoint."""
        import kiro_crew.session_pid as sp

        source = "\n".join(
            Path(str(sys.modules[name].__file__)).read_text(encoding="utf-8")
            for name in (sp.__name__, *sp._OWNERS)
        )
        assert ".write_text(" not in source, (
            "session_pid.py must rewrite PID files through _rewrite_pid_file(): "
            "Path.write_text truncates the file before writing, so a failure "
            "mid-write silently drops entries and leaks their runtimes until "
            "the host reboots."
        )


class TestAcquiringAPidLockDoesNotTruncateTheLockFile:
    """A lock file must be opened WRITABLE but never TRUNCATING.

    ``msvcrt.locking`` needs a writable handle, so the fd cannot be opened
    ``"r"``. But ``"w"`` truncates at open, and on Windows a truncating open of a
    lock file whose first byte another holder already locked raises a sharing
    violation instead of waiting — so the contending acquirer crashes with a bare
    ``OSError`` *before* it reaches ``file_lock``, and the serialisation the lock
    exists to provide never happens. POSIX ``flock`` tolerates the truncate, which
    is why the defect is invisible on Linux and reddened only the Windows shards.

    Same defect and same fix as ``work_ledger._open_lock`` and
    ``dashboard/handlers/mcp.py``'s ``_McpFileLock``, which is already
    written this way.

    Truncation is the direct, PLATFORM-INDEPENDENT observable, and that is what
    these assert: seed the lock file with bytes, take and release the lock, and
    require the bytes to have survived. Under the old ``open(lock_path, "w")``
    every one of these fails on every platform, so the guard does not depend on
    running the suite on Windows to have teeth.
    """

    SEED = b"lock-file-content-that-must-survive"

    def test_session_pid_file_lock_preserves_the_lock_file(self, session_pid_file: Path) -> None:
        from kiro_crew.session_pid import _session_pid_file_lock, _session_pid_file_path

        lock_path = _session_pid_file_path().with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(self.SEED)

        with _session_pid_file_lock():
            pass

        assert lock_path.read_bytes() == self.SEED

    def test_pid_file_lock_preserves_the_lock_file(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _pid_file_lock, _pid_file_path

        lock_path = _pid_file_path().with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(self.SEED)

        with _pid_file_lock():
            pass

        assert lock_path.read_bytes() == self.SEED

    def test_the_periodic_sweep_preserves_the_lock_file(self, session_pid_file: Path) -> None:
        """The sweep is the site most likely to feel this in production.

        It runs on a timer while ``_track_session_pid`` contends for the same
        lock, which is exactly the interleaving a truncating open turns into a
        crash rather than a wait.
        """
        from kiro_crew.session_pid import _periodic_pid_sweep, _session_pid_file_path

        path = _session_pid_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # The sweep returns early unless the pid file exists, so it must exist
        # for the lock to be reached at all.
        path.write_text(f"{os.getpid()}:999999\n", encoding="utf-8")
        lock_path = path.with_suffix(".lock")
        lock_path.write_bytes(self.SEED)

        _periodic_pid_sweep(os.getpid(), set())

        assert lock_path.read_bytes() == self.SEED

    def test_the_lock_is_still_actually_acquired(self, pid_file: Path) -> None:
        """Guard the guard: a non-truncating open that never locks would pass above.

        ``file_lock`` is asked for the lock through the same helper the production
        path uses, so this fails if the fd stopped being writable — the failure
        mode a naive ``"r"`` fix would introduce, and the reason ``"r+"`` rather
        than ``"r"`` is the answer.
        """
        from kiro_crew.session_pid import _pid_file_path

        lock_path = _pid_file_path().with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(self.SEED)

        lock_path.touch(exist_ok=True)
        with open(lock_path, "r+") as fd:
            with platform_compat.file_lock(fd.fileno(), exclusive=True):
                pass
        assert lock_path.read_bytes() == self.SEED
