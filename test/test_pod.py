"""Tests for the pod runtime (isolated worktree test instances).

This module covers ``PodConfig`` loading, path overrides, port derivation and allocation,
name validation, the env-file merge and checkout pin, worktree resolution, unit rendering and
the boot guardrails. Nine sibling modules share its fixtures through ``pod_helpers.py``:
``test_pod_env.py`` (seed sanitization, the pod env, the pod config file, the OS home and
env-file writes), ``test_pod_ownership.py`` (unit-state queries, port ownership, health and
the token mint), ``test_pod_provision.py``, ``test_pod_refusals.py`` (recording and reporting
a terminal boot refusal), ``test_pod_teardown.py``, ``test_pod_orphans.py`` (orphan homes and
``prune``), ``test_pod_cli.py``, ``test_pod_systemd.py`` (the unit file on disk, the host-state
fence, the platform guard and the user bus) and ``test_pod_boot_settings.py``.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from pod_helpers import (  # noqa: F401 -- the fixtures register by import
    _cp,
    _fake_node_toolchain,
    _isolate_pod_host_state,
    _ready_worktree,
    _systemd_backend,
    cfg_fixture,
    requires_posix_pod_lifecycle,
)

from kiro_crew import platform_compat
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import unit as unit_mod
from kiro_crew.pod.config import (
    DEFAULT_BASE_PORT,
    DEFAULT_LIVE_PORT,
    DEFAULT_UNIT_PREFIX,
    PodConfig,
    _canonical_override,
)


class TestConfig:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k in list(os.environ):
            if k.startswith("KIROCREW_POD_"):
                monkeypatch.delenv(k, raising=False)
        c = PodConfig.load()
        assert c.base_port == DEFAULT_BASE_PORT
        assert c.live_port == DEFAULT_LIVE_PORT == 5476
        assert c.unit_prefix == DEFAULT_UNIT_PREFIX == "kirocrew-pod"
        # Git is the primary resolver — no fixed root/repo pinned by default.
        assert c.repo_hint is None
        assert c.worktrees_root is None

    @staticmethod
    def _default_parts(
        home: Path, monkeypatch: pytest.MonkeyPatch, *, mise_data_dir: str | None = None
    ) -> list[str]:
        """The default gateway PATH ``PodConfig.load`` derives for *home*."""
        for k in list(os.environ):
            if k.startswith("KIROCREW_POD_"):
                monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        if mise_data_dir is None:
            monkeypatch.delenv("MISE_DATA_DIR", raising=False)
        else:
            monkeypatch.setenv("MISE_DATA_DIR", mise_data_dir)
        return PodConfig.load().gateway_path.split(os.pathsep)

    def test_default_gateway_path_reaches_user_tool_managers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pod finds a CLI installed through a user-level tool manager.

        The pod gateway gets ONLY this PATH, so a ``glab`` behind a mise shim or
        in ``~/.volta/bin`` was invisible in every pod while the host gateway
        found it.
        """
        parts = self._default_parts(tmp_path, monkeypatch)
        assert str(tmp_path / ".local" / "share" / "mise" / "shims") in parts
        assert str(tmp_path / ".volta" / "bin") in parts
        # ~/.local/bin still leads and the system dirs still close the list.
        assert parts[0] == str(tmp_path / ".local" / "bin")
        assert parts[-2:] == ["/usr/bin", "/bin"]
        assert len(parts) == len(set(parts))

    def test_default_gateway_path_never_copies_the_live_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whatever leads the operator's PATH at install time stays out of pods."""
        planted = str(tmp_path / "worktree" / ".venv" / "bin")
        monkeypatch.setenv("PATH", os.pathsep.join([planted, "/usr/bin"]))
        assert planted not in self._default_parts(tmp_path, monkeypatch)

    def test_default_gateway_path_drops_a_relative_mise_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relative MISE_DATA_DIR would put a cwd-relative entry on the pod PATH."""
        parts = self._default_parts(tmp_path, monkeypatch, mise_data_dir="relative/mise")
        assert all(os.path.isabs(p) for p in parts)
        # The rest of the default survives the dropped entry.
        assert str(tmp_path / ".volta" / "bin") in parts

    def test_default_gateway_path_carries_the_hosts_tool_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pods and the host share one tool-manager list, so they cannot drift."""
        from kiro_crew.env import user_tool_bin_dirs

        parts = self._default_parts(tmp_path, monkeypatch)
        missing = [d for d in user_tool_bin_dirs(str(tmp_path)) if d not in parts]
        assert missing == []

    def test_explicit_pod_path_still_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_PATH", "/only/this")
        assert PodConfig.load().gateway_path == "/only/this"

    def test_env_overrides_build_a_hermetic_plane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_UNIT_PREFIX", "kirocrew-podtest")
        monkeypatch.setenv("KIROCREW_POD_BASE_PORT", "7300")
        monkeypatch.setenv("KIROCREW_POD_ROOT", "/tmp/podtest-root")
        monkeypatch.setenv("KIROCREW_POD_LIVE_PORT", "9999")
        c = PodConfig.load()
        assert c.unit_prefix == "kirocrew-podtest"
        assert c.base_port == 7300
        # Compared against the CANONICAL form of the override, not a POSIX spelling
        # of it. What the config promises is that it carries the anchored value, and
        # on Windows anchoring a driveless rooted path adds the current drive
        # (``/tmp/podtest-root`` -> ``D:/tmp/podtest-root``). That is still one
        # absolute path that every reader consumes from the serialised form, so the
        # producer/consumer agreement this canonicalisation exists for holds --
        # hardcoding the input spelling asserted the platform instead of the
        # contract, and only Windows CI noticed.
        assert c.pod_root == _canonical_override("/tmp/podtest-root")
        assert c.pod_root.is_absolute()
        assert c.live_port == 9999
        assert rt.pod_unit(c, "foo") == "kirocrew-podtest@foo.service"


class TestPathOverridesAreAnchoredNotCwdRelative:
    """A relative override must name ONE directory, whoever reads it.

    The CLI and the service-manager-booted gateway do not share a working
    directory -- the pod unit sets no ``WorkingDirectory`` on purpose, so systemd
    hands the gateway ``/`` while the CLI runs wherever the operator invoked it.
    Carried through verbatim, ``KIROCREW_POD_ROOT=tmp/pods`` therefore named two
    different trees under one name.

    That is a credential leak rather than a path annoyance: ``pod down`` reclaims
    the tree the CLI resolves while the pod's kiro-cli child mints its MCP OAuth
    grants under the tree the GATEWAY resolved, so teardown swept a directory the
    grants were never in and they survived on the real machine.
    """

    RELATIVE = "tmp/pods"

    def test_the_clis_resolution_is_what_the_gateway_inherits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RED-FIRST: the producer/consumer disagreement, along the real path.

        The contract is a ROUND TRIP, not two independent reads. Anchoring at load
        cannot make two processes that each resolve ``tmp/pods`` against their own
        CWD agree -- nothing could. What it does is make the value the CLI resolved
        survive serialisation, so the gateway reads an absolute path instead of
        re-resolving a relative one against the ``/`` systemd hands it.

        So this walks the actual sequence: the CLI loads in its own directory, its
        config is rendered to the pod-plane env, and the gateway then loads with
        exactly that env from a DIFFERENT directory. Pre-fix the unit carried
        ``tmp/pods`` verbatim and the gateway landed in a tree ``pod down`` would
        never reclaim; the grants minted there outlived the pod.
        """
        from kiro_crew.pod.config import environment_vars

        cli_cwd = tmp_path / "operator-invocation-dir"
        gateway_cwd = tmp_path / "service-manager-root"
        cli_cwd.mkdir()
        gateway_cwd.mkdir()
        monkeypatch.setenv("KIROCREW_POD_ROOT", self.RELATIVE)

        monkeypatch.chdir(cli_cwd)
        from_cli = PodConfig.load()
        pinned = environment_vars(from_cli)

        # Boot the "gateway": a clean-ish environment carrying only what the unit
        # pinned, from a working directory the operator never chose.
        monkeypatch.chdir(gateway_cwd)
        for key, val in pinned.items():
            monkeypatch.setenv(key, val)
        from_gateway = PodConfig.load()

        assert from_gateway.pod_root == from_cli.pod_root, (
            f"the unit did not carry the CLI's resolution: CLI resolved "
            f"{from_cli.pod_root}, gateway resolved {from_gateway.pod_root}"
        )
        assert from_gateway.pod_root.is_absolute()

    def test_the_resolved_pod_root_is_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("KIROCREW_POD_ROOT", self.RELATIVE)

        assert PodConfig.load().pod_root.is_absolute()

    def test_serialized_exports_carry_absolute_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The unit is where the agreement has to survive the process boundary.

        ``environment_vars`` is what the systemd and launchd backends render, so a
        relative value reaching it is a relative value pinned into the service
        definition -- resolved, at boot, against a working directory the operator
        never chose.
        """
        from kiro_crew.pod.config import environment_vars

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("KIROCREW_POD_ROOT", self.RELATIVE)
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", "tmp/pod-envs")
        monkeypatch.setenv("KIROCREW_POD_ARTIFACTS_DIR", "tmp/pod-artifacts")
        monkeypatch.setenv("KIROCREW_POD_WORKTREES_ROOT", "tmp/wts")

        out = environment_vars(PodConfig.load())

        for key in (
            "KIROCREW_POD_ROOT",
            "KIROCREW_POD_ENV_DIR",
            "KIROCREW_POD_ARTIFACTS_DIR",
            "KIROCREW_POD_WORKTREES_ROOT",
        ):
            assert key in out, f"{key} should be pinned once overridden"
            assert Path(out[key]).is_absolute(), f"{key}={out[key]!r} is not absolute"

    def test_anchoring_is_absolute_and_normalising_under_windows_rules_too(self) -> None:
        """The Windows shape of anchoring, and why it still satisfies the contract.

        ``os.path.abspath`` on Windows anchors a driveless rooted path to the CURRENT
        DRIVE, so ``/tmp/pods`` loads as ``<drive>:\\tmp\\pods``. That is what made the
        first version of the sibling test fail on Windows CI only: it compared the
        loaded value against the input spelling, which asserted the platform rather
        than the contract.

        The contract is unaffected. Anchoring still yields ONE absolute path, and the
        producer serialises that path into the unit for the consumer to read, so the
        two sides agree exactly as they do on POSIX -- which is why the fix was to
        build the expectation through ``_canonical_override`` rather than to special-case
        a drive.

        Asserted through ``ntpath`` so the rule is exercised under Windows path
        semantics on any host, but only where that is HONEST. ``ntpath.abspath`` on a
        POSIX host anchors against a cwd with no drive letter, so it can neither add a
        drive to ``/tmp/pods`` nor resolve a drive-relative ``C:tmp`` -- asserting
        either would test the emulation's fidelity rather than the rule. What holds
        under both, and is the part the contract needs, is that the result is rooted
        and normalised. The drive behaviour is recorded here in prose and covered for
        real by Windows CI running the sibling tests above.
        """
        import ntpath

        anchored = ntpath.abspath("/tmp/sub/../pods")

        assert ntpath.isabs(anchored)
        assert anchored.endswith(r"\tmp\pods"), f"not normalised under Windows rules: {anchored!r}"

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX, reason="symlink creation needs elevation on Windows"
    )
    def test_dot_segments_are_normalised_but_symlinks_are_not_rewritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``abspath``, not ``resolve()`` -- the boundary of the fix, pinned.

        Normalising ``.``/``..`` is part of naming one directory. Rewriting the
        operator's symlink spelling is NOT: two spellings of one directory already
        name one inode, so teardown scope is unaffected, and resolving would
        silently change the value pinned into a unit (every ``/tmp/...`` plane
        becomes ``/private/tmp/...`` on macOS).
        """
        real = tmp_path / "real-pods"
        real.mkdir()
        link = tmp_path / "linked-pods"
        link.symlink_to(real, target_is_directory=True)
        monkeypatch.chdir(tmp_path)

        monkeypatch.setenv("KIROCREW_POD_ROOT", "sub/../real-pods")
        assert PodConfig.load().pod_root == real

        monkeypatch.setenv("KIROCREW_POD_ROOT", str(link))
        assert PodConfig.load().pod_root == link, "the symlink spelling must survive"

    def test_the_torn_down_tree_is_the_tree_the_pod_writes_grants_into(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The consequence the finding was actually about, asserted end to end.

        ``build_pod_env`` derives ``KIROCREW_OS_HOME`` from ``cfg.home_dir(name)``
        and ``cleanup_home`` deletes a direct child of ``cfg.pod_root``, so both
        sides come from the same field -- but only once that field is anchored. The
        assertion is containment rather than equality: the grant tree is nested
        one level inside the home ``pod down`` reclaims, which is what makes a
        pod's grants exactly as ephemeral as the pod.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("KIROCREW_POD_ROOT", self.RELATIVE)
        cfg = PodConfig.load()

        env = rt.build_pod_env(cfg, cfg.home_dir("demo"), 7900, tmp_path / "checkout")
        os_home = Path(env["KIROCREW_OS_HOME"])
        reclaimed = cfg.pod_root / "demo"

        assert os_home.is_absolute()
        assert (
            reclaimed in os_home.parents
        ), f"grant tree {os_home} is outside the tree pod down reclaims ({reclaimed})"


class TestPortDerivation:
    def test_matches_posix_cksum_formula(self, cfg: PodConfig) -> None:
        # Values produced by the POSIX cksum utility. Fixed vectors keep the
        # compatibility check meaningful on hosts that do not ship that utility.
        vectors = {
            "command-palette": 3585389714,
            "pod-cli": 3014794676,
            "x": 12738659,
            "a-b_c.d": 1771403360,
        }
        for name, cks in vectors.items():
            assert rt._posix_cksum(name.encode("utf-8")) == cks
            assert rt.derive_port(cfg, name) == cfg.base_port + (cks % 199) + 1

    def test_in_band(self, cfg: PodConfig) -> None:
        for name in ["a", "bb", "ccc", "command-palette", "zzzzz"]:
            assert cfg.base_port + 1 <= rt.derive_port(cfg, name) <= cfg.base_port + 199

    def test_pinned_port_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
        c = PodConfig.load()
        (tmp_path / "pinned.env").write_text("PORT='7999'\nSEED='/x'\n")
        assert rt.derive_port(c, "pinned") == 7999


class TestPortAllocation:
    """Derivation maps 199 slots, so a collision is ordinary rather than exotic.

    Allocation is the separate question `pod up` asks once: can this port actually
    be had. Before this existed the answer was assumed, so the loser's gateway
    exited "address already in use" and crash-looped while every reader kept
    printing the shared port.
    """

    def _plane(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PodConfig:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        return PodConfig.load()

    def test_the_probe_detects_a_real_listener(self, tmp_path, monkeypatch) -> None:
        """The load-bearing primitive, against a real socket rather than a stub.

        Every other test here monkeypatches `_port_is_free` for determinism, so if
        the probe itself were wrong they would all still pass. One real bind keeps
        them honest.
        """
        import socket as _socket

        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            taken = held.getsockname()[1]
            assert rt._port_is_free(taken) is False, "a bound, listening port must read as busy"
        # Released with the socket. A port can be re-taken by anything on a shared
        # host, so this direction is asserted only as "the probe answers", not as a
        # guarantee about this specific number.
        assert isinstance(rt._port_is_free(taken), bool)

    def test_a_free_derived_port_is_used_unchanged(self, tmp_path, monkeypatch) -> None:
        c = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: True)
        port, displaced = rt.allocate_port(c, "solo")
        assert port == rt.derive_port(c, "solo")
        assert displaced is None, "nothing was displaced, so nothing should be reported"

    def test_a_busy_derived_port_falls_back_and_names_what_it_displaced(
        self, tmp_path, monkeypatch
    ) -> None:
        c = self._plane(tmp_path, monkeypatch)
        derived = rt.derive_port(c, "collider")
        monkeypatch.setattr(rt, "_port_is_free", lambda p: p != derived)
        port, displaced = rt.allocate_port(c, "collider")
        assert port != derived
        assert displaced == derived, "the caller cannot report the move without this"
        assert c.base_port + 1 <= port <= c.base_port + 199, "the fallback must stay in band"

    def test_the_fallback_is_deterministic(self, tmp_path, monkeypatch) -> None:
        # Same collision must resolve the same way every time: a fallback that
        # varied per run would reintroduce the reader disagreement from the other
        # direction.
        c = self._plane(tmp_path, monkeypatch)
        derived = rt.derive_port(c, "collider")
        monkeypatch.setattr(rt, "_port_is_free", lambda p: p != derived)
        first, _ = rt.allocate_port(c, "collider")
        second, _ = rt.allocate_port(c, "collider")
        assert first == second

    def test_the_fallback_never_lands_on_the_live_plane(self, tmp_path, monkeypatch) -> None:
        """The live gateway's port is refused for the derived port already; the
        fallback must not be the way back in."""
        import dataclasses

        c = self._plane(tmp_path, monkeypatch)
        live = c.base_port + 5  # put the live plane inside the pod band
        c = dataclasses.replace(c, live_port=live)
        # ONLY the live plane is "free", so a correct allocator finds nothing and
        # says so, rather than handing the pod the live gateway's port.
        monkeypatch.setattr(rt, "_port_is_free", lambda p: p == live)
        with pytest.raises(rt.PodError, match="no free port"):
            rt.allocate_port(c, "collider")

    def test_a_busy_pinned_port_refuses_instead_of_moving_it(self, tmp_path, monkeypatch) -> None:
        # A hand-pinned port is a deliberate choice. Relocating it silently would
        # defeat the reason it was pinned, so this is the loud path.
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        c.env_file("pinned").write_text("PORT='7999'\n")
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: False)
        with pytest.raises(rt.PodError, match="pins PORT=7999"):
            rt.allocate_port(c, "pinned")

    def test_a_free_pinned_port_is_returned_as_is(self, tmp_path, monkeypatch) -> None:
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        c.env_file("pinned").write_text("PORT='7999'\n")
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: True)
        assert rt.allocate_port(c, "pinned") == (7999, None)

    def test_an_exhausted_range_raises_loudly_naming_the_band(self, tmp_path, monkeypatch) -> None:
        # A pod that cannot get a port must not appear to start.
        c = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: False)
        with pytest.raises(rt.PodError) as exc:
            rt.allocate_port(c, "crowded")
        msg = str(exc.value)
        assert "no free port" in msg
        assert str(c.base_port + 1) in msg and str(c.base_port + 199) in msg
        assert "PORT=" in msg, "the message must name the manual escape hatch"

    def test_a_probe_that_cannot_run_refuses_instead_of_answering(self, monkeypatch) -> None:
        """ "Could not run" is not "busy", and must not be coerced into it.

        Socket creation can fail for reasons that say nothing about the port -- file
        descriptor exhaustion being the obvious one. Returning False there would
        relocate a pod on no evidence, and letting the OSError escape would be a
        traceback. `instances/port_allocator.py` settles the same question for its own
        probe: a probe that could not run propagates rather than being coerced.
        """

        def _no_fds(*_a, **_k):
            raise OSError(24, "Too many open files")

        monkeypatch.setattr(rt.socket, "socket", _no_fds)
        with pytest.raises(rt.PodError, match="could not run"):
            rt._port_is_free(7811)

    def test_a_failed_socket_option_also_refuses(self, monkeypatch) -> None:
        # The option is what makes the probe mirror the real binder, so a probe that
        # could not set it is not a probe whose answer means anything.
        real = rt.socket.socket

        class _OptionFails(real):  # type: ignore[misc, valid-type]
            def setsockopt(self, *_a, **_k):  # noqa: D102
                raise OSError(1, "Operation not permitted")

        monkeypatch.setattr(rt.socket, "socket", _OptionFails)
        with pytest.raises(rt.PodError, match="could not run"):
            rt._port_is_free(7811)

    def test_a_busy_port_is_still_answered_not_raised(self) -> None:
        # The other half: bind() failing IS a real answer and must stay a bool, or
        # every ordinary collision would become a refusal.
        import socket as _socket

        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            assert rt._port_is_free(held.getsockname()[1]) is False

    def test_up_refuses_cleanly_when_the_probe_cannot_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End to end: the refusal must reach the operator as a message, not a
        # traceback, through the PodError path `_up` already handles.
        c = self._plane(tmp_path, monkeypatch)

        def _no_fds(*_a, **_k):
            raise OSError(24, "Too many open files")

        monkeypatch.setattr(rt.socket, "socket", _no_fds)
        with pytest.raises(rt.PodError, match="could not run"):
            rt.allocate_port(c, "demo")

    def test_the_probe_mirrors_the_binders_reuseaddr(self, monkeypatch) -> None:
        """The probe must be no stricter than the process it probes FOR.

        A pod's gateway binds through aiohttp's TCPSite, which leaves
        `reuse_address` at the asyncio default (set on POSIX), so it can bind a port
        whose only occupant is a TIME_WAIT remnant. Without the option here, every
        `pod down` + `pod up` would read the previous gateway's TIME_WAIT as a
        collision and relocate the pod for nothing -- the transient occupant that
        turns into a permanent move.

        The TIME_WAIT state itself is not reliably reproducible in a unit test
        (which side retains it depends on who closes first), so the mirror is pinned
        at the option instead of at the symptom.
        """
        seen: list[tuple[int, int, int]] = []
        real_socket = rt.socket.socket

        class _Recording(real_socket):  # type: ignore[misc, valid-type]
            def setsockopt(self, level, optname, value):  # noqa: D102
                seen.append((level, optname, value))
                return super().setsockopt(level, optname, value)

        monkeypatch.setattr(rt.socket, "socket", _Recording)
        rt._port_is_free(0)  # port 0 always binds; we only care about the options
        assert (
            rt.socket.SOL_SOCKET,
            rt.socket.SO_REUSEADDR,
            1,
        ) in seen, "the probe must set SO_REUSEADDR to match what the gateway binds with"

    def test_an_automatic_pin_is_relocated_when_it_goes_busy(self, tmp_path, monkeypatch) -> None:
        """The auto-pin must not become a permanent trap.

        A pin this allocator wrote records itself via PORT_AUTO. Without that, one
        transient occupant of the derived port would move the pod permanently, and
        every later collision on the fallback would hit the "a pin you set is never
        moved" refusal -- a rationale written for DELIBERATE pins governing a
        machine-written one.
        """
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        c.env_file("moved").write_text(f"PORT='7850'\n{rt.AUTO_PORT_KEY}='7850'\n")
        monkeypatch.setattr(rt, "_port_is_free", lambda p: p != 7850)
        port, displaced = rt.allocate_port(c, "moved")
        assert port != 7850, "our own busy pin must be relocated, not refused"
        assert displaced == 7850, "the move must still be reported"

    def test_a_stale_auto_marker_does_not_capture_an_operator_pin(
        self, tmp_path, monkeypatch
    ) -> None:
        # The marker stores the VALUE, so an operator who hand-edits PORT= to
        # something else stops matching it and gets operator treatment. A bare flag
        # would have let a stale marker reclassify their deliberate choice as ours.
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        c.env_file("mine").write_text(f"PORT='7999'\n{rt.AUTO_PORT_KEY}='7850'\n")
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: False)
        with pytest.raises(rt.PodError, match="pins PORT=7999"):
            rt.allocate_port(c, "mine")

    @pytest.mark.parametrize("bad", ["70000", "0", "99999999"])
    def test_an_unusable_pin_is_refused_not_crashed(self, tmp_path, monkeypatch, bad) -> None:
        """`_pinned_port` only checks for digits, so nonsense reaches the probe.

        `bind()` answers a port above 65535 with OverflowError -- NOT an OSError, so
        it would escape the probe as a traceback rather than a refusal. Port 0 is the
        opposite failure: it binds happily to an EPHEMERAL port, so it would read as
        free and the pod would be told to come up somewhere nobody can predict,
        which is the opposite of what pinning is for. Both are operator typos and
        both must be named, not guessed at.
        """
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        c.env_file("typo").write_text(f"PORT='{bad}'\n")
        with pytest.raises(rt.PodError, match="not a usable port"):
            rt.allocate_port(c, "typo")

    def test_the_probe_never_raises_on_an_impossible_port(self) -> None:
        # Defence in depth behind the range check above: the probe is the last place
        # a bad port could turn into a traceback, so it answers rather than raising.
        assert rt._port_is_free(70000) is False
        assert rt._port_is_free(-1) is False

    def test_the_walk_skips_a_port_another_pod_has_recorded(self, tmp_path, monkeypatch) -> None:
        """A probe answers "is anyone listening", not "is that port somebody's".

        A STOPPED pod listens on nothing, so its operator-pinned port probes free.
        Landing a displaced pod there boots fine and then hard-fails the pinned pod's
        next `up` with the "a pin you set is never moved" refusal -- for a squat this
        code would have created, in a message that blames the operator.

        The same read closes most of the concurrent-start race: a pod's port is
        written to disk BEFORE its unit is started, so a claim is visible here even
        while its gateway (units are Type=simple) has not bound yet.
        """
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        derived = rt.derive_port(c, "mover")
        neighbour = derived + 1  # exactly where the walk would land first
        c.env_file("neighbour").write_text(f"PORT='{neighbour}'\n")
        # Nothing is LISTENING anywhere: only the recorded claim can steer the walk.
        monkeypatch.setattr(rt, "_port_is_free", lambda p: p != derived)

        port, displaced = rt.allocate_port(c, "mover")

        assert displaced == derived
        assert port != neighbour, (
            f"the walk landed on :{neighbour}, which pod 'neighbour' has recorded -- "
            "a stopped pod's pin probes free, so the probe alone cannot see it"
        )

    def test_a_recorded_claim_beats_a_probe_that_cannot_see_it_yet(
        self, tmp_path, monkeypatch
    ) -> None:
        """THE race this fix exists for, pinned deterministically.

        Two DIFFERENT names colliding mod 199 is the precondition for the whole bug
        (same-name concurrency is already serialized by the per-name mutex, so it
        cannot express this). A unit is `Type=simple`, so `start_pod` returns before
        the gateway binds -- meaning the first pod's port still probes FREE while it
        is starting. Without a recorded claim the second allocation is handed the
        same port and one gateway crash-loops, which is the original defect
        reappearing through the concurrent door.

        Nothing is listening anywhere here on purpose: the claim is the only thing
        that can separate them, so the probe cannot mask a regression.
        """
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: True)
        shared = c.base_port + 42
        # Two names that both resolve to one port, as a mod-199 collision does.
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: shared)

        first, _ = rt.allocate_port(c, "alpha")
        # `_up` records the claim before starting; stand in for exactly that.
        rt.write_env_file(c, "alpha", {"PORT": str(first), rt.AUTO_PORT_KEY: str(first)})
        second, displaced = rt.allocate_port(c, "beta")

        assert first == shared, "the first pod should get the port it derives"
        assert second != first, (
            "the second pod was handed the port the first has already claimed -- "
            "nothing is listening yet, so only the recorded claim can prevent this"
        )
        assert displaced == shared, "the second pod should report what it moved off"

    def test_concurrent_allocations_never_hand_out_one_port_twice(
        self, tmp_path, monkeypatch
    ) -> None:
        """The same property under real threads, claim-then-record under the lock.

        Serialised by `pod_plane_mutex`, each thread records its claim before
        releasing, so the next one sees it. Asserted on DISTINCTNESS rather than on
        any particular assignment, because which name wins the lock is legitimately
        arbitrary.
        """
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: True)
        shared = c.base_port + 17
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: shared)

        names = [f"pod{i}" for i in range(6)]
        got: dict[str, int] = {}
        errors: list[BaseException] = []

        def _claim(nm: str) -> None:
            try:
                with rt.pod_plane_mutex(c):
                    port, _ = rt.allocate_port(c, nm)
                    rt.write_env_file(c, nm, {"PORT": str(port), rt.AUTO_PORT_KEY: str(port)})
                    got[nm] = port
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        workers = [threading.Thread(target=_claim, args=(n,)) for n in names]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=30)

        assert not errors, f"allocation raised under contention: {errors!r}"
        assert len(got) == len(names), f"some threads did not finish: {got}"
        assert len(set(got.values())) == len(
            names
        ), f"one port was handed out twice under contention: {got}"

    def test_every_allocation_records_a_claim_even_undisplaced(self, tmp_path, monkeypatch) -> None:
        # The claim is what the concurrent case reads, so it cannot be conditional on
        # having moved -- an undisplaced pod that records nothing is invisible to a
        # colliding name until its gateway binds.
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: True)
        port, displaced = rt.allocate_port(c, "undisplaced")
        assert displaced is None, "nothing was busy, so nothing moved"
        rt.write_env_file(c, "undisplaced", {"PORT": str(port), rt.AUTO_PORT_KEY: str(port)})
        assert rt._ports_claimed_by_other_pods(c, "someone-else").get(port) == "undisplaced"

    def test_the_walk_ignores_the_allocating_pods_own_record(self, tmp_path, monkeypatch) -> None:
        # Its own pin must not exclude it from its own band walk, or relocating an
        # auto-pin could never reuse a port the pod itself had recorded.
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        c.env_file("solo").write_text(f"PORT='7850'\n{rt.AUTO_PORT_KEY}='7850'\n")
        assert 7850 not in rt._ports_claimed_by_other_pods(c, "solo")
        assert rt._ports_claimed_by_other_pods(c, "other").get(7850) == "solo"

    def test_a_malformed_neighbour_env_does_not_block_allocation(
        self, tmp_path, monkeypatch
    ) -> None:
        # Refusing to allocate because some unrelated pod's file is unreadable would
        # be a worse failure than the collision the scan avoids.
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        c.env_file("broken").write_text("PORT='not-a-number'\nGARBAGE\n")
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: True)
        port, displaced = rt.allocate_port(c, "fine")
        assert port == rt.derive_port(c, "fine") and displaced is None

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="mkfifo is POSIX-only")
    def test_the_claim_scan_cannot_be_hung_by_a_fifo(self, tmp_path, monkeypatch) -> None:
        """A named pipe in the pods directory must not stall the plane.

        O_NOFOLLOW does NOT cover this: a FIFO is not a symlink. A plain O_RDONLY open
        of one BLOCKS until a writer appears, so a single FIFO named `*.env` would hang
        every `pod up` indefinitely -- strictly worse than the symlink case, which
        merely read the wrong bytes.

        Driven on a worker thread with a join timeout because the failure mode IS a
        hang: without O_NONBLOCK this would not fail, it would never finish.
        """
        import threading

        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        os.mkfifo(c.pods_dir / "stall.env")

        done: list[int | None] = []

        def _scan() -> None:
            done.append(rt._peer_effective_port(c, "stall", c.pods_dir / "stall.env"))

        worker = threading.Thread(target=_scan, daemon=True)
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), (
            "the claim scan blocked on a FIFO -- a single named pipe in the pods "
            "directory would hang every pod up on this plane"
        )
        assert done == [None], "a FIFO is not a pod env file, so it claims nothing"

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="mkfifo is POSIX-only")
    def test_a_fifo_does_not_stop_the_rest_of_the_scan(self, tmp_path, monkeypatch) -> None:
        # The scan must skip it and still see a real neighbour's claim: refusing the
        # whole plane because one entry is odd is the failure this guards against.
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        os.mkfifo(c.pods_dir / "stall.env")
        c.env_file("real").write_text("PORT='7877'\n")
        assert rt._ports_claimed_by_other_pods(c, "mine") == {7877: "real"}

    def test_a_directory_named_like_an_env_file_is_refused(self, tmp_path, monkeypatch) -> None:
        # Same non-regular check, reachable on every platform.
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        (c.pods_dir / "adir.env").mkdir()
        assert rt._peer_effective_port(c, "adir", c.pods_dir / "adir.env") is None

    def test_the_scan_leaks_no_descriptors(self, tmp_path, monkeypatch) -> None:
        """Every branch closes its fd, including the non-regular refusal.

        Worth pinning rather than assuming: an un-runnable probe RAISES precisely
        because descriptor exhaustion is a real state, so a scan leaking one
        per peer would be feeding the very failure it sits in front of.

        Asserted by spying on `os.open`/`os.close` and pairing what THIS scan
        acquired under the pods directory, rather than by a `/proc/self/fd`
        census: that census measures the whole xdist worker at two instants, so
        two descriptors merely open at the second sample -- a pool thread holding
        a file and a socket, a log rotation, a metrics export -- fail a pod-port
        test for a reason that has nothing to do with pods. The pairing needs no
        repeat loop either; the loop only existed to out-amplify census noise.
        """
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        (c.pods_dir / "adir.env").mkdir()
        c.env_file("real").write_text("PORT='7877'\n")

        opened: list[int] = []
        closed: list[int] = []
        real_open, real_close = os.open, os.close

        def tracking_open(target, *args, **kwargs):
            fd = real_open(target, *args, **kwargs)
            if str(target).startswith(str(c.pods_dir)):
                opened.append(fd)
            return fd

        def tracking_close(fd):
            closed.append(fd)
            return real_close(fd)

        # A private context, not the shared `monkeypatch` fixture: that instance is
        # the one `_plane` pins this plane's env vars through, and undoing it here
        # would take the plane down with the spies.
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(os, "open", tracking_open)
            patched.setattr(os, "close", tracking_close)
            assert rt._ports_claimed_by_other_pods(c, "mine") == {7877: "real"}

        # The leak property itself, which holds on every platform: whatever this
        # scan opened under the pods directory, it also closed.
        assert opened, "the scan opened nothing -- the spy missed the read seam"
        assert set(opened) <= set(closed), f"the scan leaked a descriptor: {opened} vs {closed}"
        # The DIRECTORY branch is POSIX-only, and that is a property of the OS
        # rather than of this scan: Windows has no `O_DIRECTORY` and refuses
        # `os.open` on a directory outright, so the non-regular refusal there is
        # reached by the open RAISING and never holds a descriptor at all. Only
        # where a directory can be opened is there a second fd to account for --
        # and that branch, which rejects only after opening, is where a leak
        # would hide, so it is worth pinning exactly where it exists.
        if os.name != "nt":
            assert len(opened) == 2, f"expected one open per peer entry, got {opened}"

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX, reason="symlink creation needs elevation on Windows"
    )
    def test_the_claim_scan_refuses_to_follow_a_symlinked_peer_env(
        self, tmp_path, monkeypatch
    ) -> None:
        """The scan picks its paths from directory contents, not from an operator.

        It also runs in the trusted CLI, outside the hooks gate that governs an
        agent's own reads -- so a symlink planted in the pods directory would make a
        privileged process read its target. The open is O_NOFOLLOW, so the link is
        skipped rather than followed, and allocation continues.
        """
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        secret = tmp_path / "secret-elsewhere"
        secret.write_text("PORT='7850'\n")  # a real port, so following it would SHOW
        (c.pods_dir / "sneaky.env").symlink_to(secret)

        assert (
            rt._peer_effective_port(c, "sneaky", c.pods_dir / "sneaky.env") is None
        ), "a symlinked peer env must not be read at all"
        # And the claim it would have contributed must be absent, so the scan is not
        # merely safe but also uninfluenced by the link.
        assert 7850 not in rt._ports_claimed_by_other_pods(c, "mine")

    def test_an_oversized_numeric_claim_cannot_crash_the_scan(self, tmp_path, monkeypatch) -> None:
        """`str.isdigit` admits arbitrarily long runs; `int()` refuses over 4300
        digits with ValueError. Unguarded, one unrelated pod's file would take down
        every allocation on the plane."""
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        c.env_file("huge").write_text(f"PORT='{'1' * 4301}'\n")
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: True)

        assert rt._peer_effective_port(c, "huge", c.pods_dir / "huge.env") == rt.derive_port(
            c, "huge"
        ), "an unusable stored value resolves to the derivation, as derive_port does"
        # The real assertion: allocation still works rather than raising.
        port, _ = rt.allocate_port(c, "victim")
        assert port == rt.derive_port(c, "victim")

    @pytest.mark.parametrize(
        "stored", ["7877", "0", "70000", "99999", "", "1" * 4301, "\u00b2", "\u00bd"]
    )
    def test_a_peers_claim_is_exactly_what_that_peer_resolves(
        self, tmp_path, monkeypatch, stored
    ) -> None:
        """The invariant, stated once instead of guessed per value.

        A claim is not "some port we liked the look of" -- it is the port that peer
        would actually come up on. So it must agree with `derive_port` for every
        stored value, whatever shape that value is: a usable pin resolves to the pin,
        and anything unusable (absent, out of range, oversized, non-decimal) resolves
        to the derivation, because that is what `derive_port` itself falls back to.

        Asserting the AGREEMENT rather than a hardcoded number is what makes this
        survive a change to either side.
        """
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        c.env_file("peer").write_text(f"PORT='{stored}'\n")
        assert rt._peer_effective_port(c, "peer", c.env_file("peer")) == rt.derive_port(c, "peer")

    def test_a_pre_upgrade_pod_with_no_pin_is_still_claimed(self, tmp_path, monkeypatch) -> None:
        """The upgrade window: a pod that predates claims is running, unclaimed.

        Before this change nothing recorded a `PORT=`, so every already-running pod has
        an env file with a CHECKOUT and no port. Reading that as "claims nothing" would
        leave exactly those pods unprotected: during a restart gap the pod is listening
        on nothing, so the probe reports its port free too, and a colliding name would
        take it and leave the legacy pod crash-looping on EADDRINUSE.

        Resolving what the peer WOULD use closes that window without requiring every
        pod to be restarted under the new code first.
        """
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        # Exactly a pre-upgrade file: a checkout pin, no PORT.
        c.env_file("legacy").write_text("CHECKOUT='/some/worktree'\n")
        legacy_port = rt.derive_port(c, "legacy")

        claimed = rt._ports_claimed_by_other_pods(c, "newcomer")
        assert claimed.get(legacy_port) == "legacy", (
            "a pod with no recorded PORT is still running on its derived port; "
            "treating it as unclaimed is what lets a colliding name evict it"
        )

        # And the walk must honour it even with nothing listening anywhere -- which is
        # precisely the restart-gap state.
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: True)
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: legacy_port)
        port, displaced = rt.allocate_port(c, "newcomer")
        assert port != legacy_port, "the newcomer took a running legacy pod's port"
        assert displaced == legacy_port

    def test_an_unreadable_peer_claims_nothing(self, tmp_path, monkeypatch) -> None:
        # The other side of the derivation fallback: it applies only to a file we could
        # READ. A hostile entry must not be able to reserve a port by name alone.
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        (c.pods_dir / "ghost.env").mkdir()  # non-regular: unreadable
        assert rt._peer_effective_port(c, "ghost", c.pods_dir / "ghost.env") is None
        assert rt.derive_port(c, "ghost") not in rt._ports_claimed_by_other_pods(c, "mine")

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="byte filenames are a POSIX concept")
    def test_a_non_utf8_peer_filename_cannot_crash_the_scan(self, tmp_path, monkeypatch) -> None:
        """Filenames are BYTES on POSIX, so a peer name need not be valid UTF-8.

        Such a name arrives decoded with surrogates, and the derivation fallback has to
        ENCODE the name to hash it -- which raises UnicodeEncodeError on a surrogate.
        Measured: `bad-\\xff.env` is handed over as `'bad-\\udcff.env'`, and encoding
        that stem raises. Unguarded, one such file would traceback every `pod up` on the
        plane.

        Guarded at the NAME rather than at the encode: a stem that is not a legal pod
        name is not a pod, so it has no port to claim and nothing to read.
        """
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        target = os.path.join(os.fsencode(str(c.pods_dir)), b"bad-\xff.env")
        try:
            os.close(os.open(target, os.O_CREAT | os.O_WRONLY, 0o600))
        except (OSError, ValueError) as exc:
            # Not every POSIX filesystem allows arbitrary bytes in a name: APFS
            # enforces UTF-8 and refuses this with EILSEQ, so macOS cannot stage the
            # scenario at all. Skip rather than platform-guess -- what matters is
            # whether THIS filesystem can hold such a name, not which OS we are on.
            pytest.skip(f"this filesystem refuses a non-UTF-8 filename: {exc}")
        c.env_file("real").write_text("PORT='7877'\n")

        # Must not raise, and must still see the legitimate peer beside it.
        assert rt._ports_claimed_by_other_pods(c, "mine") == {7877: "real"}

        monkeypatch.setattr(rt, "_port_is_free", lambda _p: True)
        port, _ = rt.allocate_port(c, "mine")  # the real assertion: no traceback
        assert port == rt.derive_port(c, "mine")

    @pytest.mark.parametrize("stem", ["-leading", "has space", "a" * 70, "", "..", "a/b"])
    def test_only_legal_pod_names_are_treated_as_peers(self, tmp_path, monkeypatch, stem) -> None:
        # The precondition stated as a rule rather than as a list of crashes: peers are
        # pods, so a stem that `validate_name` would reject claims nothing.
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        try:
            (c.pods_dir / f"{stem}.env").write_text("PORT='7877'\n")
        except (OSError, ValueError):
            pytest.skip(f"the filesystem will not hold a {stem!r} entry")
        assert 7877 not in rt._ports_claimed_by_other_pods(c, "mine")

    @pytest.mark.parametrize(
        "value,expected,why",
        [
            ("7999", 7999, "a plain decimal run is a port"),
            # U+00B2 SUPERSCRIPT TWO: isdigit() True, isdecimal() False, int() raises.
            # Written as an escape so this file stays ASCII and the codepoint is
            # explicit rather than depending on the reader's font.
            ("\u00b2", None, "isdigit() is True but int() raises -- the round-9 crash"),
            # U+0662 ARABIC-INDIC DIGIT TWO: decimal, and int() parses it as 2.
            ("\u0662", 2, "Arabic-Indic digits ARE decimal and int() parses them"),
            # U+00BD VULGAR FRACTION ONE HALF: neither digit nor decimal.
            ("\u00bd", None, "neither digit nor decimal"),
            ("1" * 4301, None, "int() refuses a conversion over 4300 digits"),
            ("70000", 70000, "out of range but PARSEABLE: allocate_port names it"),
            ("", None, "absent"),
            (None, None, "key not present at all"),
        ],
    )
    def test_the_shared_parser_defines_what_counts_as_a_port(self, value, expected, why) -> None:
        """One parser, one answer, every character class pinned.

        Guarding this per call site gets it wrong differently -- on length, on a
        sibling that was never audited, and on character class. The parser is a
        single function, so these cases are asserted once here instead of per site.

        `isdecimal` rather than `isdigit` is the whole point: `isdigit` is not the
        predicate that matches `int()`. And `isascii` would have been wrong in the
        other direction -- it rejects U+0662 ARABIC-INDIC DIGIT TWO, which `int()`
        parses as 2.
        """
        assert rt._port_from_env(value) == expected, why

    def test_no_env_derived_digits_can_crash_a_port_read(self, tmp_path, monkeypatch) -> None:
        """The class, asserted through every consumer of the shared parser.

        These are reached by read-only verbs (`pod url`, `pod ls`, Dev Fleet) as well
        as by `pod up`, so a hand-edited env file must not traceback out of any of
        them. Kept alongside the parser's own table because what matters is that each
        CONSUMER routes through it.
        """
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        for label, raw in (
            ("huge", "1" * 4301),
            ("uni", "\u00b2"),  # SUPERSCRIPT TWO -- digit but not decimal
            ("frac", "\u00bd"),  # VULGAR FRACTION ONE HALF -- neither
        ):
            c.env_file(label).write_text(f"PORT='{raw}'\n{rt.AUTO_PORT_KEY}='{raw}'\n")
            assert rt._pinned_port(c, label) is None, f"{label}: pin must not parse"
            expected = c.base_port + (rt._posix_cksum(label.encode()) % 199) + 1
            assert (
                rt.derive_port(c, label) == expected
            ), f"{label}: an unusable pin must fall back to derivation, not traceback"
            assert (
                rt.operator_pinned(c, label) is False
            ), f"{label}: a value that is not a port is not a pin at all"
            assert rt._peer_effective_port(c, label, c.pods_dir / f"{label}.env") == rt.derive_port(
                c, label
            ), (
                f"{label}: an unusable stored value must resolve to the derivation, "
                "which is what derive_port falls back to"
            )

    def test_an_oversized_auto_marker_does_not_crash_the_provenance_check(
        self, tmp_path, monkeypatch
    ) -> None:
        # The marker is attacker-shaped in exactly the same way as the pin: it is a
        # hand-editable digit string, so it must never reach int() either.
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        c.env_file("weird").write_text(f"PORT='7999'\n{rt.AUTO_PORT_KEY}='{'9' * 4301}'\n")
        assert (
            rt.operator_pinned(c, "weird") is True
        ), "a marker that does not match the pin means the pin is the operator's"

    def test_the_plane_lock_borrows_the_name_mutex_rather_than_copying_it(
        self, tmp_path, monkeypatch
    ) -> None:
        """One lock implementation, so the two cannot drift apart.

        The plane lock's key must be exactly what `pod_name_mutex` produces for the
        reserved name, and that name must be unreachable as a real pod so the two
        can never collide -- `_NAME_RE` forbids '@'.
        """
        import contextlib as _contextlib

        c = self._plane(tmp_path, monkeypatch)
        taken: list[str] = []
        real = rt.pod_name_mutex

        @_contextlib.contextmanager
        def _recording(cfg, nm):
            taken.append(nm)
            with real(cfg, nm):
                yield

        monkeypatch.setattr(rt, "pod_name_mutex", _recording)
        with rt.pod_plane_mutex(c):
            pass

        assert taken == [rt._PLANE_LOCK_NAME], "the plane lock must go through pod_name_mutex"
        with pytest.raises(rt.PodError):
            rt.validate_name(rt._PLANE_LOCK_NAME)  # unreachable as a real pod name


class TestNameValidation:
    @pytest.mark.parametrize("bad", ["", "../x", "a/b", "x" * 70, "-leading", "a b"])
    def test_rejects(self, bad: str) -> None:
        with pytest.raises(rt.PodError):
            rt.validate_name(bad)

    @pytest.mark.parametrize("good", ["command-palette", "a.b_c-d", "x", "A1"])
    def test_accepts(self, good: str) -> None:
        assert rt.validate_name(good) == good


class TestEnvFileAndPin:
    def test_write_merge_preserves_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
        c = PodConfig.load()
        rt.write_env_file(c, "x", {"CHECKOUT": "/a", "PORT": "7999"})
        rt.write_env_file(c, "x", {"SEED": "/s"})  # merge, don't clobber
        assert rt.read_env_file(c, "x") == {"CHECKOUT": "/a", "PORT": "7999", "SEED": "/s"}

    def test_pin_checkout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
        c = PodConfig.load()
        rt.pin_checkout(c, "x", Path("/abs/co"))
        assert rt.read_env_file(c, "x")["CHECKOUT"] == "/abs/co"


class TestWorktreeResolution:
    """Git-native: a friendly name → checkout via pinned CHECKOUT, else git, else
    an optional root, else a teaching error."""

    def test_git_worktrees_parses_porcelain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        out = (
            "worktree /repo/main\nHEAD aaa\nbranch refs/heads/main\n\n"
            "worktree /repo/kirocrew-wt-foo\nHEAD bbb\nbranch refs/heads/feat/foo\n\n"
        )
        monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _cp(stdout=out))
        wts = rt._git_worktrees(Path("/repo/main"))
        assert wts["main"] == Path("/repo/main")
        assert wts["kirocrew-wt-foo"] == Path("/repo/kirocrew-wt-foo")
        assert wts["feat/foo"] == Path("/repo/kirocrew-wt-foo")  # branch match

    def test_git_worktrees_empty_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _cp(returncode=128))
        assert rt._git_worktrees(Path("/nope")) == {}

    def test_resolve_prefers_pin(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        c = PodConfig.load()
        co = tmp_path / "co"
        co.mkdir()
        rt.pin_checkout(c, "demo", co)
        # Even if git would resolve elsewhere, a valid pin wins (boot path).
        monkeypatch.setattr(rt, "_git_worktrees", lambda ref: {"demo": Path("/other")})
        assert rt.resolve_checkout(c, "demo", cwd=tmp_path) == co

    def test_resolve_via_git_basename_and_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        c = PodConfig.load()
        monkeypatch.setattr(
            rt, "_git_worktrees", lambda ref: {"foo": Path("/x/foo"), "feat/bar": Path("/x/bar")}
        )
        assert rt.resolve_checkout(c, "foo", cwd=tmp_path) == Path("/x/foo")
        assert rt.resolve_checkout(c, "bar", cwd=tmp_path) == Path("/x/bar")  # feat/<name>

    def test_resolve_root_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setenv("KIROCREW_POD_WORKTREES_ROOT", str(tmp_path / "wts"))
        c = PodConfig.load()
        (tmp_path / "wts" / "demo").mkdir(parents=True)
        monkeypatch.setattr(rt, "_git_worktrees", lambda ref: {})
        assert rt.resolve_checkout(c, "demo", cwd=tmp_path) == tmp_path / "wts" / "demo"

    def test_resolve_raises_teaching(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        c = PodConfig.load()
        monkeypatch.setattr(rt, "_git_worktrees", lambda ref: {})
        with pytest.raises(rt.PodError, match="git worktree add"):
            rt.resolve_checkout(c, "ghost", cwd=tmp_path)

    def test_stale_pin_falls_through_to_git(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        c = PodConfig.load()
        rt.pin_checkout(c, "demo", tmp_path / "gone")  # pinned dir is absent
        (tmp_path / "real").mkdir()
        monkeypatch.setattr(rt, "_git_worktrees", lambda ref: {"demo": tmp_path / "real"})
        assert rt.resolve_checkout(c, "demo", cwd=tmp_path) == tmp_path / "real"


def _uncommented(unit_text: str) -> str:
    """The unit's directive lines only — comments explain the absent hook by name,
    so a substring scan over the whole file cannot tell prose from configuration."""
    return "\n".join(ln for ln in unit_text.splitlines() if not ln.lstrip().startswith("#"))


class TestUnitRendering:
    def test_execstart_reenters_pod_run(self, cfg: PodConfig) -> None:
        assert "pod _run %i" in unit_mod.render_unit(cfg)

    def test_unit_has_no_teardown_hook(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ExecStopPost runs BEFORE systemd's final kill of the unit's cgroup, so a
        teardown hook there deleted the HOME under the pod's own surviving
        subprocesses — and fired on the stop half of a Restart=, restarting the pod
        onto a wiped home. Reclamation belongs to `pod down` (runtime.stop_pod)."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", "/tmp/podtest-root")
        txt = unit_mod.render_unit(PodConfig.load())
        directives = [
            ln.split("=", 1)[0]
            for ln in txt.splitlines()
            if "=" in ln and not ln.lstrip().startswith("#")
        ]
        assert "ExecStopPost" not in directives
        assert "pod _cleanup" not in _uncommented(txt)
        # ...and teardown never becomes a raw recursive remove in the unit either.
        assert "-rf" not in txt and "$HOME" not in txt
        # Self-heal is still wanted: it is only safe BECAUSE the hook is gone.
        assert "Restart=on-failure" in txt

    def test_env_block_pins_nondefaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", "/tmp/hermetic-pods")
        monkeypatch.setenv("KIROCREW_POD_REPO", "/tmp/some-repo")
        txt = unit_mod.render_unit(PodConfig.load())
        # Same reason as TestConfig's canonical comparison: the unit pins the
        # ANCHORED value, which on Windows carries the current drive.
        root = _canonical_override("/tmp/hermetic-pods")
        repo = _canonical_override("/tmp/some-repo")
        assert f'Environment="KIROCREW_POD_ROOT={root}"' in txt
        assert f'Environment="KIROCREW_POD_REPO={repo}"' in txt

    def test_env_block_quotes_spaces(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", "/tmp/pod root")
        cfg = PodConfig.load()
        txt = unit_mod.render_unit(cfg)
        root_line = next(line for line in txt.splitlines() if "KIROCREW_POD_ROOT=" in line)
        assert root_line.startswith('Environment="')
        assert "pod root" in root_line

    def test_unit_path_uses_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_UNIT_PREFIX", "kirocrew-podtest")
        assert unit_mod.unit_path(PodConfig.load()).name == "kirocrew-podtest@.service"

    @requires_posix_pod_lifecycle
    def test_install_unit_refuses_a_symlinked_template(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        destination = tmp_path / "pod@.service"
        victim = tmp_path / "victim.service"
        victim.write_text("keep")
        destination.symlink_to(victim)
        monkeypatch.setattr(unit_mod, "unit_path", lambda _cfg: destination)

        with pytest.raises(OSError, match="symbolic link"):
            unit_mod.install_unit(cfg)

        assert victim.read_text() == "keep"


class TestBootGuardrails:
    def test_refuses_no_pinned_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        assert rt.boot(PodConfig.load(), "nope") == 3

    def test_refuses_missing_venv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        co = tmp_path / "co"
        co.mkdir()
        rt.pin_checkout(c, "x", co)  # pinned but no .venv → exit 3
        assert rt.boot(c, "x") == 3

    def test_refuses_live_port(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        co = _ready_worktree(tmp_path, "x")
        rt.pin_checkout(c, "x", co)
        with patch.object(rt, "derive_port", return_value=c.live_port):
            assert rt.boot(c, "x") == 70
