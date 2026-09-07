"""Tests for the pod runtime (isolated worktree test instances)."""

from __future__ import annotations

import argparse
import ast
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.pod import cli as pod_cli
from kiro_crew.pod import provision as prov
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import unit as unit_mod
from kiro_crew.pod.config import (
    DEFAULT_BASE_PORT,
    DEFAULT_LIVE_PORT,
    DEFAULT_UNIT_PREFIX,
    PodConfig,
)

# The invoking user's REAL home, captured at import time — before the autouse
# fixture below pins HOME to a tmp dir. Used only to assert that pod host state
# never lands there (see TestHostStateIsFenced).
_REAL_HOME = Path.home()

# Stand-in for a version-manager node bin dir (mise/nvm/fnm/volta/asdf install
# under $HOME). Provisioning resolves ``npm`` to an absolute path there.
NODE_BIN = "/fake/node/bin"

requires_posix_pod_lifecycle = pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="pod lifecycle requires POSIX descriptor traversal",
)


def _npm(*args: str) -> list[str]:
    """The argv provisioning is expected to spawn for an npm step."""
    return [f"{NODE_BIN}/npm", *args]


@pytest.fixture(autouse=True)
def _fake_node_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin node-toolchain resolution so provisioning tests are host-independent.

    ``provision`` now resolves ``npm`` to an absolute path and hands the child a
    PATH carrying the node bin dir (npm run-scripts are ``#!/usr/bin/env node``).
    Without this fixture the tests below would pass or fail according to whether
    the host running them happens to have a version manager installed.
    """
    monkeypatch.setattr(prov, "find_node_tool", lambda name: f"{NODE_BIN}/{name}")
    monkeypatch.setattr(
        prov, "node_augmented_path", lambda base="": f"{NODE_BIN}{os.pathsep}{base}"
    )


@pytest.fixture(autouse=True)
def _isolate_pod_host_state(tmp_path_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep pod HOST state out of the developer's real home.

    Two pod paths resolve through ``Path.home()`` rather than ``KIROCREW_HOME``, so
    conftest's safety net does not cover them, and both are written by ordinary
    test paths:

    ``unit.unit_path`` — a test reaching ``install_unit`` (via ``_up`` ->
    ``install_backend``, or the ``start_pod`` self-heal) rewrites the REAL
    ``kirocrew-pod@.service`` with this test's tmpdir as
    ``Environment=KIROCREW_POD_ROOT=``. Every later ``pod up`` on the host then dies
    with "no pinned checkout", so running the suite breaks pods for everyone until
    someone re-runs ``pod install``. Observed on a real host.

    ``PodConfig.pods_dir`` — resolves off the DEFAULT data home on purpose (a pod
    process must not be able to redirect the host's pod registry into its own
    throwaway home). The per-name lifecycle mutex writes its lock file there.

    Pinning ``HOME`` fixes the cause both share instead of patching each function
    that reads it, so a future path that resolves through the home is covered
    without a matching change here. ``USERPROFILE`` too, because Windows
    ``Path.home()`` reads that one. Tests that pin either themselves still win.
    """
    home = tmp_path_factory.mktemp("pod-host-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


@pytest.fixture(autouse=True)
def _systemd_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the SYSTEMD backend by default, on every host.

    ``runtime`` dispatches unit operations on ``IS_MACOS``, so without this the
    tests that monkeypatch ``rt.systemctl`` would silently exercise the launchd
    branch when the suite runs on a Mac — passing on Linux CI and failing (or
    worse, vacuously passing) on a developer's laptop. Pinning it makes the
    default explicit and keeps the Linux/Windows contract asserted everywhere.
    Tests for the macOS path set ``IS_MACOS`` True themselves.
    """
    monkeypatch.setattr(rt, "IS_MACOS", False)


@pytest.fixture
def cfg() -> PodConfig:
    return PodConfig.load()


def _cp(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _ready_worktree(
    root: Path, name: str, *, venv: bool = True, dist: bool = True, flags: bool = True
) -> Path:
    """Build a flat kirocrew worktree checkout (repo root == worktree dir).

    ``flags`` writes a ``cli.py`` declaring the gateway flags a CURRENT checkout
    declares. ``boot`` probes the target's own source before passing a flag to it
    (the control plane and the target worktree are independent checkouts), so a
    fixture without it models an OLD checkout -- pass ``flags=False`` to exercise
    that arm deliberately rather than by omission.
    """
    co = root / name
    co.mkdir(parents=True, exist_ok=True)
    if venv:
        # Mirror the real per-platform venv layout so the detectors are
        # exercised against what a venv on THIS host actually looks like.
        b = prov.venv_bin(co)
        b.parent.mkdir(parents=True, exist_ok=True)
        b.write_text("#!/bin/sh\n")
        b.chmod(0o755)
    if dist:
        (co / "src" / "kiro_crew" / "static" / "dist").mkdir(parents=True, exist_ok=True)
    if flags:
        cli = co / "src" / "kiro_crew" / "cli.py"
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.write_text('gw.add_argument("--no-crons")\ngw.add_argument("--no-tunnel")\n')
    return co


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

    def test_env_overrides_build_a_hermetic_plane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_UNIT_PREFIX", "kirocrew-podtest")
        monkeypatch.setenv("KIROCREW_POD_BASE_PORT", "7300")
        monkeypatch.setenv("KIROCREW_POD_ROOT", "/tmp/podtest-root")
        monkeypatch.setenv("KIROCREW_POD_LIVE_PORT", "9999")
        c = PodConfig.load()
        assert c.unit_prefix == "kirocrew-podtest"
        assert c.base_port == 7300
        assert c.pod_root == Path("/tmp/podtest-root")
        assert c.live_port == 9999
        assert rt.pod_unit(c, "foo") == "kirocrew-podtest@foo.service"


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

    @pytest.mark.skipif(
        rt.fcntl is None,
        reason=(
            "pod_name_mutex -- which pod_plane_mutex borrows -- degrades to a no-op "
            "without fcntl, so nothing serializes these threads there. Not a gap: "
            "require_backend refuses pods on any host without systemd/launchd, so "
            "production never reaches the no-op. Only this test could."
        ),
    )
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

        Worth pinning rather than assuming: round 10 made an un-runnable probe RAISE
        precisely because descriptor exhaustion is a real state, so a scan leaking one
        per peer would be feeding the very failure it sits in front of.
        """
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        (c.pods_dir / "adir.env").mkdir()
        c.env_file("real").write_text("PORT='7877'\n")

        def _open_fds() -> int:
            return len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else -1

        before = _open_fds()
        for _ in range(50):
            rt._ports_claimed_by_other_pods(c, "mine")
        after = _open_fds()
        if before != -1:
            assert after <= before + 1, f"descriptors grew from {before} to {after}"

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

        Three call sites used to guard this themselves and each got it wrong
        differently -- on length, on a sibling that was never audited, and on
        character class. The guard is now a single function, so these cases are
        asserted once here instead of being rediscovered per site.

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
        rt.pin_checkout(c, "demo", tmp_path / "gone")  # pinned dir no longer exists
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
        assert 'Environment="KIROCREW_POD_ROOT=/tmp/hermetic-pods"' in txt
        assert 'Environment="KIROCREW_POD_REPO=/tmp/some-repo"' in txt

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


class TestSeedSanitization:
    """Deny-by-default: a seeded pod must NEVER boot with tunnel enabled."""

    def _write(self, d: Path, obj: object) -> Path:
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps(obj))
        return d

    def test_forces_tunnel_off_when_enabled(self, tmp_path: Path) -> None:
        seed = self._write(tmp_path / "s", {"tunnel": {"enabled": True, "name": "keep"}})
        out = rt.sanitized_seed_config(seed)
        assert out is not None and out["tunnel"]["enabled"] is False
        assert out["tunnel"]["name"] == "keep"  # other keys preserved

    def test_overwrites_non_dict_tunnel(self, tmp_path: Path) -> None:
        seed = self._write(tmp_path / "s", {"tunnel": True})
        out = rt.sanitized_seed_config(seed)
        assert out is not None and out["tunnel"] == {"enabled": False}

    def test_adds_tunnel_when_absent(self, tmp_path: Path) -> None:
        seed = self._write(tmp_path / "s", {"dashboard": {"port": 5476}})
        out = rt.sanitized_seed_config(seed)
        assert out is not None and out["tunnel"]["enabled"] is False

    def test_forces_agent_sandbox_floor_and_preserves_other_agent_keys(
        self, tmp_path: Path
    ) -> None:
        seed = self._write(
            tmp_path / "s",
            {
                "agent": {
                    "sandbox": "off",
                    "sandbox_allow_unsandboxed_exec": True,
                    "sandbox_allow_no_isolation": True,
                    "bot_name": "keep",
                }
            },
        )
        out = rt.sanitized_seed_config(seed)
        assert out is not None
        assert out["agent"]["sandbox"] == "auto"
        assert out["agent"]["sandbox_allow_unsandboxed_exec"] is False
        assert out["agent"]["sandbox_allow_no_isolation"] is False
        assert out["agent"]["bot_name"] == "keep"

    def test_bad_json_returns_none(self, tmp_path: Path) -> None:
        seed = tmp_path / "s"
        seed.mkdir()
        (seed / "config.json").write_text("{not valid json")
        assert rt.sanitized_seed_config(seed) is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert rt.sanitized_seed_config(tmp_path / "nope") is None

    def test_non_dict_root_returns_none(self, tmp_path: Path) -> None:
        assert rt.sanitized_seed_config(self._write(tmp_path / "s", [1, 2, 3])) is None

    def test_refuses_sensitive_seed_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = self._write(tmp_path / "s", {"tunnel": {"enabled": True}})
        monkeypatch.setattr("kiro_crew.security.is_sensitive_path", lambda p, base_dir=None: True)
        assert rt.sanitized_seed_config(seed) is None

    def test_forces_teams_off_and_keeps_its_app_id(self, tmp_path: Path) -> None:
        """A pod that inherited ``teams.enabled=true`` boots the operator's REAL
        Azure Bot: the App ID rides along in config.json and the secret arrives
        from the inherited env, so it would answer real people in their org."""
        seed = self._write(tmp_path / "s", {"teams": {"enabled": True, "app_id": "azure-live"}})
        out = rt.sanitized_seed_config(seed)
        assert out is not None and out["teams"]["enabled"] is False
        assert out["teams"]["app_id"] == "azure-live"  # preserved, just disabled

    def test_forces_every_disabled_section_off(self, tmp_path: Path) -> None:
        """Both shapes at once, for every section: an enabled dict, and a non-dict
        value that would otherwise let the ``enabled=False`` write be skipped."""
        sections = rt.SEED_DISABLED_SECTIONS
        seeded = {s: ({"enabled": True} if i % 2 else True) for i, s in enumerate(sections)}
        out = rt.sanitized_seed_config(self._write(tmp_path / "s", seeded))
        assert out is not None
        still_on = [s for s in sections if out[s].get("enabled") is not False]
        assert not still_on, f"sections a seeded pod could boot live: {still_on}"

    def test_disabled_sections_cover_every_rostered_channel(self) -> None:
        """The ratchet: a channel added to the roster with a config-level enable
        must be named here, or a seeded pod boots it against real users. Teams
        reached the roster without reaching this list, which is the hole.

        A channel with no ``enabled`` field is credential-gated only (Slack), and
        those credentials are scrubbed by ``build_pod_env`` — but the exempt set is
        pinned exactly, so a new credential-gated channel is an explicit decision
        rather than a silent omission.
        """
        import dataclasses

        from kiro_crew.channels import builtin_channel_descriptors
        from kiro_crew.config.loader import KiroCrewConfig

        section_fields = {f.name: f for f in dataclasses.fields(KiroCrewConfig)}
        exempt: set[str] = set()
        ungated: list[str] = []
        for desc in builtin_channel_descriptors():
            section = desc.channel_type
            factory = section_fields[section].default_factory
            if "enabled" not in getattr(factory, "__dataclass_fields__", {}):
                exempt.add(section)
            elif section not in rt.SEED_DISABLED_SECTIONS:
                ungated.append(section)
        assert not ungated, f"channels a seeded pod could boot live: {ungated}"
        assert exempt == {"slack"}


class TestPodEnvCredentialScrub:
    """The pod env must carry no credential that can act as a live messaging
    identity — ``pod_context()`` hands this same env to arbitrary commands."""

    def test_scrubs_the_teams_app_credentials(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """None of the three Teams vars ends in ``_TOKEN`` or starts with a scrubbed
        channel prefix, so the generic suffix rule that catches every other bot
        credential lets the Azure Bot secret through to the pod gateway."""
        monkeypatch.setenv("MICROSOFT_APP_ID", "app-live")
        monkeypatch.setenv("MICROSOFT_APP_PASSWORD", "secret-live")
        monkeypatch.setenv("MICROSOFT_APP_TENANT_ID", "tenant-live")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "sts-temp")
        env = rt.build_pod_env(cfg, tmp_path / "home", 7999, tmp_path / "co")
        assert "MICROSOFT_APP_ID" not in env
        assert "MICROSOFT_APP_PASSWORD" not in env
        assert "MICROSOFT_APP_TENANT_ID" not in env
        assert env.get("AWS_SESSION_TOKEN") == "sts-temp"  # AWS kept on purpose

    def test_scrubs_every_channel_credential_the_loader_knows(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ratchet for the scrub itself: it matches on prefix and suffix, so a
        credential named like neither reaches the pod silently — which is exactly
        how ``MICROSOFT_APP_*`` survived. Pin it against the loader's roster.

        Two keys are kept deliberately: ``KIRO_API_KEY`` is the agent's own model
        credential and a pod runs agent turns, and ``KIROCREW_OWNER_ID`` is the pod
        dashboard's owner identity rather than a bot credential.
        """
        from kiro_crew.config.loader import (
            _CREDENTIAL_KEYS,
            CRED_KIRO_API_KEY,
            CRED_OWNER_ID,
        )

        for key in _CREDENTIAL_KEYS:
            monkeypatch.setenv(key, f"live-{key}")
        env = rt.build_pod_env(cfg, tmp_path / "home", 7999, tmp_path / "co")
        survivors = set(_CREDENTIAL_KEYS) & set(env)
        assert survivors == {CRED_KIRO_API_KEY, CRED_OWNER_ID}

    @pytest.mark.parametrize("inherited", ["::1", "0.0.0.0", "192.168.1.5"])
    def test_bind_is_pinned_to_the_loopback_every_pod_client_dials(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inherited: str
    ) -> None:
        """An inherited KIROCREW_BIND must never reach the pod gateway.

        `health()`, `mint_token()` and `pod_api()` all dial `127.0.0.1:<port>`,
        and the ownership attestation vouches for the recorded PROCESS, not for
        the ADDRESS it bound. A gateway inheriting `::1` would serve the IPv6
        loopback while a foreign local process binds `127.0.0.1:<port>` — the
        address the mint's own HTTP request then lands on. `0.0.0.0` (the
        official image's export) would additionally expose the pod beyond the
        host. The pin is what keeps listener and attestation on one address.
        """
        monkeypatch.setenv("KIROCREW_BIND", inherited)
        env = rt.build_pod_env(cfg, tmp_path / "home", 7999, tmp_path / "co")
        assert env["KIROCREW_BIND"] == "127.0.0.1"


class TestWaitHealthyFailsFast:
    def test_bails_on_failed_unit(self, cfg: PodConfig) -> None:
        with (
            patch.object(rt, "health", return_value=0),
            patch.object(rt, "unit_state", return_value=("failed", 0)),
        ):
            assert pod_cli._wait_healthy(cfg, "x", 7999, tries=45) == -1

    def test_bails_on_crash_loop(self, cfg: PodConfig) -> None:
        with (
            patch.object(rt, "health", return_value=0),
            patch.object(rt, "unit_state", return_value=("activating", 1)),
        ):
            assert pod_cli._wait_healthy(cfg, "x", 7999, tries=45) == -1

    def test_returns_code_when_healthy(self, cfg: PodConfig) -> None:
        with (
            patch.object(rt, "health", return_value=403),
            patch.object(rt, "unit_state", return_value=("active", 0)),
        ):
            assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) == 403

    def test_a_foreign_port_holder_outranks_the_generic_crash_verdict(self, cfg: PodConfig) -> None:
        """A taken port is WHY the gateway crash-loops, so say that instead.

        Both signals are present in this scenario -- the port answers from
        somebody else AND the unit is restarting behind it -- and only one of them
        tells the operator what to do about it.
        """
        with (
            patch.object(rt, "health", return_value=rt.HEALTH_FOREIGN),
            patch.object(rt, "unit_state", return_value=("activating", 1)),
        ):
            assert pod_cli._wait_healthy(cfg, "x", 7999, tries=45) == rt.HEALTH_FOREIGN

    def test_a_foreign_holder_seen_mid_wait_is_remembered_at_the_deadline(
        self, cfg: PodConfig
    ) -> None:
        """The verdict survives a later poll that merely fails to connect."""
        codes = iter([rt.HEALTH_FOREIGN, 0, 0])

        def _health(*a: object, **k: object) -> int:
            return next(codes, 0)

        with (
            patch.object(rt, "health", _health),
            patch.object(rt, "unit_state", return_value=("active", 0)),
        ):
            assert pod_cli._wait_healthy(cfg, "x", 7999, tries=2) == rt.HEALTH_FOREIGN

    def test_a_pod_that_wins_its_port_back_still_reports_healthy(self, cfg: PodConfig) -> None:
        """Seeing a squatter first must not condemn a pod that then comes up.

        A predecessor releasing the port mid-wait is an ordinary sequence, so the
        foreign observation is attribution for a FAILURE, never a verdict of its
        own.
        """
        codes = iter([rt.HEALTH_FOREIGN, 200])

        def _health(*a: object, **k: object) -> int:
            return next(codes, 200)

        with (
            patch.object(rt, "health", _health),
            patch.object(rt, "unit_state", return_value=("active", 0)),
        ):
            assert pod_cli._wait_healthy(cfg, "x", 7999, tries=5) == 200


class TestPortOwner:
    """Identity, not reachability: WHO holds the pod's derived port.

    A derived port maps 199 slots across every pod name and can be pinned by
    hand, so a collision with another pod or with the live gateway is ordinary.
    """

    @pytest.fixture(autouse=True)
    def _posix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin the POSIX branch, for the same reason the module pins ``IS_MACOS``.

        ``port_owner`` returns ``OWNER_UNPROVEN`` on a non-POSIX host before it
        consults any of the helpers these tests patch, so on Windows the decision
        cases FAILED (`'unproven' == 'pod'`) while the four that expect
        ``OWNER_UNPROVEN`` passed VACUOUSLY -- they would have passed whatever the
        logic did. Pinning it makes the branch under test explicit and asserts the
        same contract on every platform. ``test_non_posix_is_unproven`` sets it
        False itself, which still wins: this fixture runs first.
        """
        monkeypatch.setattr(rt, "IS_POSIX", True)
        # Every positive ownership verdict requires the gateway's pid sidecar to
        # agree with the service manager's MainPID. Patching the reader stands in
        # for a record that PROVED fresh (it answers ``None`` otherwise), which is
        # what lets these cases vary listener evidence independently; the
        # freshness proof itself is covered in ``test_pod_api.py``.
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda cfg, name, port: 4242)

    @staticmethod
    def _listener(pid: int, address: str = "127.0.0.1", family: str = "4"):
        return platform_compat.PortListener(pid=pid, address=address, family=family)

    def test_the_pods_own_main_pid_holding_the_port_is_proof_it_is_ours(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(rt, "find_port_listeners", lambda port: [self._listener(4242)])
        monkeypatch.setattr(rt, "main_pid", lambda c, n: 4242)
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_POD

    def test_a_wildcard_bind_by_the_pod_still_counts_as_ours(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """0.0.0.0 receives a 127.0.0.1 connect, so a wildcard pod owns the probe."""
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(
            rt, "find_port_listeners", lambda port: [self._listener(4242, "0.0.0.0")]
        )
        monkeypatch.setattr(rt, "main_pid", lambda c, n: 4242)
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_POD

    def test_ownership_is_scoped_to_the_address_the_probe_reached(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pod existing elsewhere on the port must not vouch for a squatter.

        A port NUMBER can carry several LISTEN sockets on different local
        addresses. Here the pod holds one specific non-loopback address while a
        foreign process holds 127.0.0.1 -- the address the probe dialled -- so the
        foreign pid is the responder. Taking every pid on the port number instead
        would find the pod's own and trust the squatter BECAUSE the real pod
        exists.
        """
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(
            rt,
            "find_port_listeners",
            lambda port: [
                self._listener(4242, "10.0.0.5"),  # the pod, on another address
                self._listener(9999, "127.0.0.1"),  # the squatter, on the probed one
            ],
        )
        monkeypatch.setattr(rt, "main_pid", lambda c, n: 4242)
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_FOREIGN

    def test_a_different_pid_holding_the_port_is_foreign(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(rt, "find_port_listeners", lambda port: [self._listener(9999)])
        monkeypatch.setattr(rt, "main_pid", lambda c, n: 4242)
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_FOREIGN

    def test_a_pod_with_no_process_on_a_held_port_is_foreign_not_unproven(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported bug, at its root.

        A pod that lost the bind race has no process at all. If "no pid of ours"
        read as undecidable, the squatter's 200 would still be reported as this
        pod's health -- which is the whole defect. A held port plus a pod that
        authoritatively has no pid IS proof the responder is someone else.
        """
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(rt, "find_port_listeners", lambda port: [self._listener(9999)])
        monkeypatch.setattr(rt, "main_pid", lambda c, n: None)
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_FOREIGN

    def test_missing_main_pid_is_unproven_without_listener_tool(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rt, "main_pid", lambda c, n: None)
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: False)
        monkeypatch.setattr(
            rt, "find_port_listeners", lambda port: pytest.fail("must not be reached")
        )
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_UNPROVEN

    def test_non_posix_is_unproven(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rt, "IS_POSIX", False)
        monkeypatch.setattr(
            rt, "listening_pid_tool_available", lambda: pytest.fail("must not be reached")
        )
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_UNPROVEN

    def test_an_unaskable_service_manager_is_unproven(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "Could not ask" must never be rendered as "not ours"."""

        def _boom(c: object, n: object) -> int:
            raise rt.PodError("systemctl unavailable")

        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(rt, "find_port_listeners", lambda port: [self._listener(9999)])
        monkeypatch.setattr(rt, "main_pid", _boom)
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_UNPROVEN

    def test_a_throwing_listener_lookup_keeps_a_fresh_pid_attestation(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken lookup says nothing about who holds the port.

        The autouse fixture supplies a record that PROVED fresh, so downgrading
        it here would punish the pod for the tool's failure. What is refused is
        an UNPROVABLE record -- see ``TestPodRecordFreshness`` in
        ``test_pod_api.py`` for that leg.
        """

        def _boom(port: int) -> list:
            raise OSError("lsof exploded")

        monkeypatch.setattr(rt, "main_pid", lambda c, n: 4242)
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(rt, "find_port_listeners", _boom)
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_POD

    def test_an_unattributable_listener_keeps_a_fresh_pid_attestation(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An installed tool can still fail to attribute the reached socket.

        Exactly what an unprivileged caller sees when the socket belongs to a
        gateway its own process cannot inspect, which is how every pod runs.
        """
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(rt, "find_port_listeners", lambda port: [])
        monkeypatch.setattr(rt, "main_pid", lambda c, n: 4242)
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_POD

    @pytest.mark.parametrize("tool_available, listeners", [(False, None), (True, [])])
    def test_an_unprovable_record_withholds_the_pod_secret(
        self,
        cfg: PodConfig,
        monkeypatch: pytest.MonkeyPatch,
        tool_available: bool,
        listeners: list[platform_compat.PortListener] | None,
    ) -> None:
        """No listener evidence AND no provable record: the secret stays put.

        ``_pod_recorded_pid`` answering ``None`` is what a crash leftover, a
        pre-binding record, and a host that will not report a start time all
        collapse to, so this covers the residual refusal on every one of them.
        """
        home = cfg.home_dir("demo")
        home.mkdir(parents=True)
        (home / ".local_secret").write_text("must-not-leave-this-process")
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda cfg, name, port: None)
        monkeypatch.setattr(rt, "main_pid", lambda c, n: 4242)
        monkeypatch.setattr(rt, "derive_port", lambda c, n: 7999)
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: tool_available)
        if listeners is not None:
            monkeypatch.setattr(rt, "find_port_listeners", lambda port: listeners)
        monkeypatch.setattr(
            rt,
            "loopback_urlopen",
            lambda *a, **k: pytest.fail("the secret must not be sent"),
        )

        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_UNPROVEN
        with pytest.raises(rt.PodOwnershipUnproven):
            rt.mint_token(cfg, "demo", "1h")


class TestMainPid:
    def test_reads_systemd_main_pid(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(stdout="MainPID=4242\n"))
        assert rt.main_pid(cfg, "demo") == 4242

    def test_main_pid_zero_means_not_running(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """systemd prints MainPID=0 for a dead or unknown unit, and exits 0."""
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(stdout="MainPID=0\n"))
        assert rt.main_pid(cfg, "demo") is None

    def test_a_missing_main_pid_line_refuses_rather_than_guessing(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No MainPID key at all means the QUERY failed, not that the pod is dead."""
        monkeypatch.setattr(
            rt, "systemctl", lambda *a, **k: _cp(stdout="", returncode=1, stderr="boom")
        )
        with pytest.raises(rt.PodError):
            rt.main_pid(cfg, "demo")


class TestProvision:
    def test_detectors(self, tmp_path: Path) -> None:
        full = _ready_worktree(tmp_path, "full", venv=True, dist=True)
        assert prov.has_venv(full) and prov.has_dist(full)
        novenv = _ready_worktree(tmp_path, "novenv", venv=False, dist=True)
        assert not prov.has_venv(novenv) and prov.has_dist(novenv)

    def test_venv_bin_follows_the_platform_venv_layout(self, tmp_path: Path) -> None:
        """``has_venv`` is called on every platform to report build state in the
        Dev Fleet view (pods themselves stay Linux-only), so a POSIX-only path
        here would report a built Windows worktree as unbuilt."""
        got = prov.venv_bin(tmp_path)
        if platform_compat.IS_WINDOWS:
            assert got == tmp_path / ".venv" / "Scripts" / "kirocrew.exe"
        else:
            assert got == tmp_path / ".venv" / "bin" / "kirocrew"

    def test_provision_venv_only_skips_build(self, tmp_path: Path) -> None:
        co = _ready_worktree(tmp_path, "be-only", venv=True, dist=False)
        with patch.object(prov, "build_dist", side_effect=AssertionError("must not build")):
            assert prov.provision(co, build=False) is True


class TestProvisionBuildPaths:
    def test_ensure_venv_builds_then_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "_find_python", lambda version="3.12": "/usr/bin/python3.12")

        def fake_run(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
            if cmd[1:3] == ["-m", "venv"]:
                b = prov.venv_bin(co)
                b.parent.mkdir(parents=True, exist_ok=True)
                b.write_text("#!/bin/sh\n")
                b.chmod(0o755)
            return 0

        monkeypatch.setattr(prov, "_run", fake_run)
        assert prov.ensure_venv(co) is True

    def test_ensure_venv_no_python(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "_find_python", lambda version="3.12": None)
        assert prov.ensure_venv(co) is False

    def test_build_dist_npm_then_stages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co = tmp_path / "wt"
        (co / "website").mkdir(parents=True)

        def fake_run(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
            (co / "website" / "dist").mkdir(parents=True, exist_ok=True)
            (co / "website" / "dist" / "index.html").write_text("<html>")
            return 0

        monkeypatch.setattr(prov, "_run", fake_run)
        assert prov.build_dist(co) is True
        # website/dist staged into the served static/dist.
        assert (co / "src" / "kiro_crew" / "static" / "dist" / "index.html").is_file()

    def test_build_dist_no_website_dir(self, tmp_path: Path) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        assert prov.build_dist(co) is False

    def test_build_dist_short_circuits_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co = _ready_worktree(tmp_path, "wt", venv=False, dist=True)
        monkeypatch.setattr(prov, "_run", lambda cmd, cwd, env=None: 99)  # must not be called
        assert prov.build_dist(co) is True

    def test_provision_full_chain(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "ensure_venv", lambda c: True)
        monkeypatch.setattr(prov, "build_dist", lambda c: True)
        assert prov.provision(co, build=True) is True

    def test_find_python_via_which(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(prov.Path, "exists", lambda self: False)
        monkeypatch.setattr(prov.shutil, "which", lambda exe: "/opt/python3.12")
        assert prov._find_python() == "/opt/python3.12"


class TestProvisionDependencyInstall:
    """Dependency-gap fixes: npm deps before dist build (#229), dev extras in
    the venv with graceful fallback for old pip (#230)."""

    def _venv_seeding_run(self, co: Path, calls: list[list[str]], group_fails: bool = False):
        """Return a fake _run that records calls and materializes the venv bin on
        `python -m venv`, so has_venv() passes. Optionally fail `--group` cmds."""

        def fake_run(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
            calls.append(cmd)
            if cmd[1:3] == ["-m", "venv"]:
                # Materialize the venv entry point at the layout THIS platform
                # actually uses, so has_venv() and the pod runtime agree.
                b = prov.venv_bin(co)
                b.parent.mkdir(parents=True, exist_ok=True)
                b.write_text("#!/bin/sh\n")
                b.chmod(0o755)
            if group_fails and "--group" in cmd:
                return 1
            return 0

        return fake_run

    # ---- #229: website npm deps ----

    def test_npm_ci_when_node_modules_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        website = tmp_path / "wt" / "website"
        website.mkdir(parents=True)
        calls: list[list[str]] = []
        monkeypatch.setattr(prov, "_run", lambda cmd, cwd, env=None: calls.append(cmd) or 0)
        assert prov.ensure_node_modules(website) is True
        assert _npm("ci") in calls
        # ci succeeded → no fallback (non-mutating install never runs)
        assert _npm("install", "--no-package-lock") not in calls

    def test_node_modules_skipped_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        website = tmp_path / "wt" / "website"
        (website / "node_modules" / ".bin").mkdir(parents=True)
        (website / "node_modules" / ".bin" / "tsc").write_text("#!/bin/sh\n")

        def boom(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
            raise AssertionError(f"must not run npm when node_modules present: {cmd}")

        monkeypatch.setattr(prov, "_run", boom)
        assert prov.ensure_node_modules(website) is True

    def test_npm_install_fallback_when_ci_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        website = tmp_path / "wt" / "website"
        website.mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
            calls.append(cmd)
            return 1 if cmd == _npm("ci") else 0  # ci fails, install succeeds

        monkeypatch.setattr(prov, "_run", fake_run)
        assert prov.ensure_node_modules(website) is True
        # Fallback must be NON-MUTATING: --no-package-lock so the tracked
        # website/package-lock.json is never rewritten (would dirty the worktree).
        assert _npm("ci") in calls
        assert _npm("install", "--no-package-lock") in calls
        assert _npm("install") not in calls  # plain (mutating) install never runs

    def test_build_dist_installs_node_modules_before_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co = tmp_path / "wt"
        website = co / "website"
        website.mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
            calls.append(cmd)
            if cmd == _npm("run", "build"):
                (website / "dist").mkdir(parents=True, exist_ok=True)
                (website / "dist" / "index.html").write_text("<html>")
            return 0

        monkeypatch.setattr(prov, "_run", fake_run)
        assert prov.build_dist(co) is True
        assert calls.index(_npm("ci")) < calls.index(_npm("run", "build"))

    # ---- #230: venv dev extras ----

    def test_pip_group_dev_attempted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "_find_python", lambda version="3.12": "/usr/bin/python3.12")
        calls: list[list[str]] = []
        monkeypatch.setattr(prov, "_run", self._venv_seeding_run(co, calls))
        assert prov.ensure_venv(co) is True
        assert any(c[-2:] == ["--group", "dev"] for c in calls)

    def test_pip_group_dev_fallback_on_old_pip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "_find_python", lambda version="3.12": "/usr/bin/python3.12")
        calls: list[list[str]] = []
        monkeypatch.setattr(prov, "_run", self._venv_seeding_run(co, calls, group_fails=True))
        assert prov.ensure_venv(co) is True
        assert any("--group" in c for c in calls)  # attempted --group dev
        pip = str(prov.venv_bin_dir(co) / ("pip.exe" if platform_compat.IS_WINDOWS else "pip"))
        assert [pip, "install", "--editable", str(co)] in calls  # then fell back


class TestPodEnv:
    def test_scrubs_slack_and_nonaws_tokens_keeps_aws(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-live-bot")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-live")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret")
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "sts-temp")
        env = rt.build_pod_env(cfg, tmp_path / "home", 7999, tmp_path / "co")
        assert "SLACK_BOT_TOKEN" not in env and "SLACK_APP_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env  # non-AWS *_TOKEN scrubbed
        # AWS_* kept (agent turns need it); AWS_SESSION_TOKEN must survive.
        assert env.get("AWS_REGION") == "us-west-2"
        assert env.get("AWS_SESSION_TOKEN") == "sts-temp"
        assert env["KIROCREW_PORT"] == "7999"
        assert env["KIROCREW_HOME"].endswith("home")
        assert env["KIROCREW_PROJECT_DIR"].endswith("co")


class TestPodConfigWrite:
    def test_blank_pod_writes_tunnel_off_config(self, tmp_path: Path) -> None:
        home = tmp_path / "pod-home"
        rt.write_pod_config(home, seed="")
        data = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert data["tunnel"]["enabled"] is False

    def test_config_and_home_are_owner_only(self, tmp_path: Path) -> None:
        home = tmp_path / "pod-home"
        rt.write_pod_config(home, seed="")
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
        assert stat.S_IMODE((home / "config.json").stat().st_mode) == 0o600

    def test_seed_config_sanitized_and_locked_down(self, tmp_path: Path) -> None:
        seed = tmp_path / "seed"
        seed.mkdir()
        (seed / "config.json").write_text(
            json.dumps({"tunnel": {"enabled": True}, "provider_token": "secret"})
        )
        home = tmp_path / "pod-home"
        rt.write_pod_config(home, seed=str(seed))
        data = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert data["tunnel"]["enabled"] is False  # sanitized
        assert stat.S_IMODE((home / "config.json").stat().st_mode) == 0o600

    def test_a_failed_lockdown_publishes_no_config(self, tmp_path: Path, monkeypatch) -> None:
        """restrict_to_owner runs on the temp; a failure must not leave config.json."""
        monkeypatch.setattr(
            "kiro_crew.atomic_write.platform_compat.restrict_to_owner",
            lambda path: (_ for _ in ()).throw(OSError("icacls: transient failure")),
        )
        home = tmp_path / "pod-home"
        with pytest.raises(OSError, match="icacls"):
            rt.write_pod_config(home, seed="")
        assert not (home / "config.json").exists()
        assert not list(home.glob("*.tmp"))


class TestCleanupHome:
    def test_removes_pod_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        victim = c.home_dir("demo")
        victim.mkdir(parents=True)
        (victim / "marker").write_text("x")
        assert rt.cleanup_home(c, "demo") == 0
        assert not victim.exists()
        assert c.pod_root.exists()  # parent untouched

    def test_refuses_dotdot_escape(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pods = tmp_path / "pods"
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pods))
        c = PodConfig.load()
        pods.mkdir(parents=True)
        sentinel = tmp_path / "DO_NOT_DELETE"  # lives in pod_root's PARENT
        sentinel.write_text("precious")
        assert rt.cleanup_home(c, "..") != 0
        assert sentinel.exists() and pods.exists()


class TestCleanupHomeVerifies:
    """``rmtree`` runs with ``ignore_errors`` (a partially-removed tree is still
    progress), so ``cleanup_home``'s return value is the only signal a caller has
    that the HOME is actually gone. A constant 0 is what let ``pod down`` print
    "zero residue" over a directory still on disk."""

    def _held_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[PodConfig, Path]:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / "security_events.jsonl").write_text("{}\n")
        # A pod-scoped process still holds the tree (or reopens its audit log in
        # append mode right behind the delete), so the removal achieves nothing
        # and says nothing — exactly what was measured on a live host.
        monkeypatch.setattr(rt.shutil, "rmtree", lambda *a, **k: None)
        return c, home

    def test_reports_a_home_that_survived_the_delete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c, home = self._held_home(tmp_path, monkeypatch)
        assert rt.cleanup_home(c, "demo") == 1
        out = capsys.readouterr().out
        assert str(home) in out
        # Naming the culprit is the point: a bare failure sends the operator
        # hunting for which writer resurrected the directory.
        assert "security_events.jsonl" in out

    def test_an_unlistable_survivor_still_fails_instead_of_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The survivor listing is diagnostics; it must never turn teardown into a
        traceback on a directory the process cannot read."""
        c, _ = self._held_home(tmp_path, monkeypatch)

        def _boom(self):  # noqa: ANN001 - patched onto Path
            raise PermissionError("nope")

        monkeypatch.setattr(Path, "iterdir", _boom)
        assert rt.cleanup_home(c, "demo") == 1


class TestDrainCgroup:
    """The delete waits for the unit's process tree, because that is the set that
    recreates the HOME behind it."""

    def test_a_vanished_cgroup_counts_as_drained(self, tmp_path: Path) -> None:
        assert rt.drain_cgroup(tmp_path / "gone" / "cgroup.procs") == []

    def test_waits_until_the_last_process_exits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        procs = tmp_path / "cgroup.procs"
        procs.write_text("7\n8\n")
        remaining = ["8\n", ""]
        monkeypatch.setattr(rt.time, "sleep", lambda _s: procs.write_text(remaining.pop(0)))
        assert rt.drain_cgroup(procs) == []
        assert remaining == [], "it must poll until empty, not sample once"

    def test_reports_the_survivors_when_the_window_expires(self, tmp_path: Path) -> None:
        procs = tmp_path / "cgroup.procs"
        procs.write_text("7\n8\n")
        assert rt.drain_cgroup(procs, timeout=0.0) == ["7", "8"]


@requires_posix_pod_lifecycle
class TestLinuxTeardownOrdering:
    """Reclamation lives on the ``down`` path, not in an ``ExecStopPost`` hook that
    systemd runs BEFORE the final kill of the unit's cgroup. So ``stop_pod`` owns
    the ordering: stop, drain, delete, verify."""

    def test_the_cgroup_is_read_before_the_stop(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """systemd reports an empty ``ControlGroup`` for an inactive unit, so asking
        after the stop returns nothing and the drain silently degrades to a no-op."""
        order: list[str] = []

        def _read_cgroup(c: PodConfig, n: str) -> None:
            order.append("read-cgroup")
            return None

        def _systemctl(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
            order.append(args[0])
            return _cp()

        monkeypatch.setattr(rt, "cgroup_procs_file", _read_cgroup)
        monkeypatch.setattr(rt, "systemctl", _systemctl)
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
        # No teardown hook loaded, so no refresh interleaves with the ordering.
        monkeypatch.setattr(rt, "loaded_teardown_hook", lambda c, n: False)
        assert rt.stop_pod(cfg, "demo").returncode == 0
        assert order == ["read-cgroup", "stop"]

    def test_a_stale_unit_is_refreshed_before_the_stop(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A unit installed by an older build still carries the destructive
        ExecStopPost, and `systemctl stop` runs it BEFORE our drain — so the first
        `down` after an upgrade would delete the HOME under the pod's own live
        processes, losing its sessions and config. Refreshing must therefore happen
        before the stop, not just before a start."""
        order: list[str] = []

        def _systemctl(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
            order.append(args[0])
            return _cp()

        monkeypatch.setattr(rt, "loaded_teardown_hook", lambda c, n: True)
        monkeypatch.setattr(rt.unit_mod, "install_unit", lambda c: order.append("re-render"))
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda c, n: None)
        monkeypatch.setattr(rt, "systemctl", _systemctl)
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
        assert rt.stop_pod(cfg, "demo").returncode == 0
        assert order == ["re-render", "daemon-reload", "stop"]

    def test_a_current_unit_is_not_re_rendered_on_every_stop(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reverse guard: re-rendering unconditionally would daemon-reload on
        every single `pod down`."""
        rendered: list[str] = []
        monkeypatch.setattr(rt.unit_mod, "unit_is_current", lambda c: True)
        monkeypatch.setattr(rt.unit_mod, "install_unit", lambda c: rendered.append("re-render"))
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda c, n: None)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp())
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
        assert rt.stop_pod(cfg, "demo").returncode == 0
        assert rendered == []

    def _stale_unit_with_failing_reload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> list[str]:
        """A stale unit on disk (it carries the removed directive) plus a
        daemon-reload that fails. Returns the list systemctl verbs are recorded in."""
        unit_file = tmp_path / "pod@.service"
        unit_file.write_text("[Service]\nExecStart=/x\nExecStopPost=/y pod _cleanup %i\n")
        monkeypatch.setattr(rt.unit_mod, "unit_path", lambda c: unit_file)
        monkeypatch.setattr(rt.unit_mod, "_kirocrew_argv", lambda: (sys.executable,))
        issued: list[str] = []

        def _systemctl(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
            issued.append(args[0])
            if args[0] == "daemon-reload":
                return _cp(returncode=1, stderr="Failed to reload daemon")
            if args[0] == "show":  # systemd has the destructive hook loaded
                return _cp(stdout="{ path=/x ; argv[]=pod _cleanup x }\n")
            return _cp()

        monkeypatch.setattr(rt, "systemctl", _systemctl)
        return issued

    def test_the_stop_trusts_systemd_not_the_unit_file(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disk freshness is not proof of a load. A hookless file can sit in front
        of a cached definition that still deletes the HOME — reached by ANY writer
        whose reload failed, `pod install` included. So the stop asks systemd what
        it will execute, and refreshes on that."""
        order: list[str] = []

        def _systemctl(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
            order.append(args[0])
            if args[0] == "show":
                # systemd still has the destructive hook loaded.
                return _cp(stdout="{ path=/usr/bin/kirocrew ; argv[]=pod _cleanup x }\n")
            return _cp()

        # The unit file on disk looks perfectly current — the old gate's signal.
        monkeypatch.setattr(rt.unit_mod, "unit_is_current", lambda c: True)
        monkeypatch.setattr(rt.unit_mod, "install_unit", lambda c: order.append("re-render"))
        monkeypatch.setattr(rt, "systemctl", _systemctl)
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda c, n: None)
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
        assert rt.stop_pod(cfg, "demo").returncode == 0
        assert order == ["show", "re-render", "daemon-reload", "stop"]

    def test_an_unanswerable_hook_query_refreshes_anyway(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "Cannot tell" must read as "the hook is there"; the other way round
        silently ships the defect on any host where the query fails."""
        order: list[str] = []

        def _systemctl(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
            order.append(args[0])
            return (
                _cp(returncode=1, stderr="Failed to get properties") if args[0] == "show" else _cp()
            )

        monkeypatch.setattr(rt.unit_mod, "install_unit", lambda c: order.append("re-render"))
        monkeypatch.setattr(rt, "systemctl", _systemctl)
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda c, n: None)
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
        assert rt.stop_pod(cfg, "demo").returncode == 0
        assert "re-render" in order

    def test_no_refresh_when_systemd_reports_no_hook(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reverse guard: refreshing unconditionally would daemon-reload on
        every single `pod down`."""
        order: list[str] = []

        def _systemctl(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
            order.append(args[0])
            return _cp(stdout="\n") if args[0] == "show" else _cp()

        monkeypatch.setattr(rt.unit_mod, "install_unit", lambda c: order.append("re-render"))
        monkeypatch.setattr(rt, "systemctl", _systemctl)
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda c, n: None)
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
        assert rt.stop_pod(cfg, "demo").returncode == 0
        assert order == ["show", "stop"]
        assert "daemon-reload" not in order

    def test_a_failed_reload_refuses_the_stop_and_removes_the_fresh_unit(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed reload splits disk from systemd: the fresh hookless file is on
        disk, but systemd still executes the OLD definition. Leaving that file
        behind is the real damage — `unit_is_current` reads disk, so every later
        call would report "current" while a crash restart still wipes the HOME.
        So remove it, and do not stop a pod whose loaded unit still has the hook."""
        issued = self._stale_unit_with_failing_reload(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda c, n: None)
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
        cp = rt.stop_pod(cfg, "demo")
        assert cp.returncode != 0
        assert "pod install" in cp.stderr
        assert "stop" not in issued, "must not stop while systemd runs the old hook"
        assert not (tmp_path / "pod@.service").exists()
        # The invariant that matters: staleness must not read as current.
        assert rt.unit_mod.unit_is_current(cfg) is False

    def test_a_failed_reload_refuses_the_start_and_removes_the_fresh_unit(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issued = self._stale_unit_with_failing_reload(tmp_path, monkeypatch)
        cp = rt.start_pod(cfg, "demo")
        assert cp.returncode != 0
        assert "pod install" in cp.stderr
        assert "start" not in issued
        assert not (tmp_path / "pod@.service").exists()

    def test_the_delete_waits_for_the_process_tree(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        procs = tmp_path / "cgroup.procs"
        procs.write_text("4242\n")
        order: list[str] = []
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda c, n: procs)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp())

        def _last_process_exits(_s: float) -> None:
            order.append("drained")
            procs.write_text("")

        def _delete(c: PodConfig, n: str) -> int:
            order.append("deleted")
            return 0

        monkeypatch.setattr(rt.time, "sleep", _last_process_exits)
        monkeypatch.setattr(rt, "cleanup_home", _delete)
        assert rt.stop_pod(cfg, "demo").returncode == 0
        assert order == ["drained", "deleted"], "deleting first is the original race"

    def test_both_teardown_messages_name_the_same_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """A failed teardown prints twice — once from `stop_pod`, once from
        `cleanup_home` — and the two must name the HOME identically. `pod_home`
        returns it unresolved while `cleanup_home` resolves before deleting, so on
        a host whose home is a symlink (the standard dev-desktop layout) the same
        directory appeared as `/home/...` and `/local/home/...` and read as two
        locations. Reproduced here with a symlinked pod root."""
        real = tmp_path / "real-pods"
        real.mkdir()
        link = tmp_path / "linked-pods"
        link.symlink_to(real)
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(link))
        c = PodConfig.load()
        (c.home_dir("demo")).mkdir(parents=True)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp())
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda cc, n: None)
        monkeypatch.setattr(rt.shutil, "rmtree", lambda *a, **k: None)
        cp = rt.stop_pod(c, "demo")
        assert cp.returncode != 0
        resolved = str((real / "demo").resolve())
        assert resolved in cp.stderr
        assert resolved in capsys.readouterr().out, "cleanup_home must agree"
        assert str(link / "demo") not in cp.stderr, "the second spelling is the bug"

    def test_a_failed_stop_leaves_a_possibly_live_pods_state_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / "kirocrew.db").write_text("live")
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=1, stderr="job failed"))
        cp = rt.stop_pod(c, "demo")
        assert cp.returncode != 0
        assert (home / "kirocrew.db").read_text() == "live"

    def test_a_process_outliving_the_drain_blocks_the_delete_entirely(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deleting after the drain EXPIRES would be the original defect wearing a
        verification: the live writer recreates the directory in append mode right
        behind the delete, which lands after the check, so `down` reports zero
        residue over a HOME that comes back. Refuse to delete instead."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / "kirocrew.db").write_text("a writer still has this")
        deleted: list[str] = []
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp())
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda cc, n: tmp_path / "cgroup.procs")
        monkeypatch.setattr(rt, "drain_cgroup", lambda procs, **k: ["4242", "4243"])
        monkeypatch.setattr(rt, "cleanup_home", lambda cc, n: deleted.append(n) or 0)
        cp = rt.stop_pod(c, "demo")
        assert cp.returncode != 0
        assert deleted == [], "must not delete a HOME a live process is still in"
        assert (home / "kirocrew.db").read_text() == "a writer still has this"
        assert str(home) in cp.stderr
        assert "NOT zero-residue" in cp.stderr
        # The pids that would not exit are the actionable part of the report.
        assert "4242" in cp.stderr and "4243" in cp.stderr

    def test_a_surviving_home_is_reported_not_called_zero_residue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drained cleanly, but the delete still did not take (permissions, or a
        writer outside the cgroup). The verification is what catches this."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp())
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda cc, n: tmp_path / "cgroup.procs")
        monkeypatch.setattr(rt, "drain_cgroup", lambda procs, **k: [])
        monkeypatch.setattr(rt.shutil, "rmtree", lambda *a, **k: None)
        cp = rt.stop_pod(c, "demo")
        assert cp.returncode != 0
        assert str(home) in cp.stderr
        assert "NOT zero-residue" in cp.stderr


@requires_posix_pod_lifecycle
class TestTheUnitFileNeverOutlivesAFailedLoad:
    """The invariant behind three separate defects: a unit file present on disk has
    been loaded by systemd. Every writer must uphold it — fixing only the lifecycle
    path left `pod install` able to strand a hookless file in front of a cached
    destructive definition, which is what made the on-disk freshness check lie."""

    def _plane(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        unit_file = tmp_path / "pod@.service"
        monkeypatch.setattr(rt.unit_mod, "unit_path", lambda c: unit_file)
        monkeypatch.setattr(rt.unit_mod, "_kirocrew_argv", lambda: (sys.executable,))
        monkeypatch.setattr(rt, "require_backend", lambda: None)
        return unit_file

    def test_install_removes_the_unit_when_the_reload_fails(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unit_file = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(
            rt, "systemctl", lambda *a, **k: _cp(returncode=1, stderr="reload failed")
        )
        msg, cp = rt.install_backend(cfg)
        assert cp is not None and cp.returncode != 0
        assert not unit_file.exists(), "an unloaded unit must not be left on disk"
        assert rt.unit_mod.unit_is_current(cfg) is False
        assert "removed" in msg

    def test_install_keeps_the_unit_when_the_reload_succeeds(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unit_file = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp())
        msg, cp = rt.install_backend(cfg)
        assert cp is not None and cp.returncode == 0
        assert unit_file.exists()
        assert rt.unit_mod.unit_is_current(cfg) is True
        assert "installed pod template unit" in msg


@requires_posix_pod_lifecycle
class TestPodNameMutexOnLinux:
    """Linux teardown moved onto the ``down`` path, so Linux now has the same
    down/up race the launchd backend needed the flock for: the mutex can no longer
    be a no-op there."""

    def test_start_and_stop_hold_it(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        import contextlib as _ctx

        held: list[str] = []

        @_ctx.contextmanager
        def _fake(c: PodConfig, n: str):
            held.append(f"enter:{n}")
            yield
            held.append(f"exit:{n}")

        monkeypatch.setattr(rt, "pod_name_mutex", _fake)
        monkeypatch.setattr(rt.unit_mod, "unit_is_current", lambda c: True)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp())
        rt.start_pod(cfg, "demo")
        assert held == ["enter:demo", "exit:demo"]

        held.clear()
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
        rt.stop_pod(cfg, "demo")
        assert held == ["enter:demo", "exit:demo"], "the sweep must run INSIDE the mutex"

    @pytest.mark.skipif(
        rt.fcntl is None,
        reason="flock needs POSIX; without it the mutex is a documented no-op",
    )
    def test_it_is_a_real_lock(self, cfg: PodConfig) -> None:
        with rt.pod_name_mutex(cfg, "demo"):
            pass
        assert (cfg.pods_dir / f"{cfg.unit_prefix}@demo.lock").exists()

    def test_boot_never_takes_the_lock(self) -> None:
        """`up` holds the mutex across the health wait, and the process it is
        waiting for is the pod's own `pod _run` -> boot(). If boot ever acquired
        the same per-name lock, `up` would wait for a gateway that is blocked on
        `up` — a deadlock no unit test would notice from either side alone.

        Checked TRANSITIVELY and in both call spellings: a bare
        `pod_name_mutex(...)`, an `rt.pod_name_mutex(...)` attribute call, or any
        module-level helper `boot` reaches that takes the lock would all deadlock
        identically, so pinning only the direct bare-name call would pin the letter
        of the rule rather than the property.
        """
        tree = ast.parse(Path(rt.__file__).read_text(encoding="utf-8"))
        funcs = {
            n.name: n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        def called_names(fn: ast.AST) -> set[str]:
            out: set[str] = set()
            for n in ast.walk(fn):
                if not isinstance(n, ast.Call):
                    continue
                if isinstance(n.func, ast.Name):
                    out.add(n.func.id)
                elif isinstance(n.func, ast.Attribute):
                    out.add(n.func.attr)
            return out

        reached: set[str] = set()
        stack = ["boot"]
        while stack:
            name = stack.pop()
            if name in reached or name not in funcs:
                continue
            reached.add(name)
            stack.extend(called_names(funcs[name]))

        assert "boot" in reached, "boot must exist for this guard to mean anything"
        assert "pod_name_mutex" not in reached
        # The lock is only ever taken by these two, so neither may be reachable
        # from boot either — that is the same deadlock one level removed.
        assert "start_pod" not in reached
        assert "stop_pod" not in reached


class TestOrphanHomes:
    """Neither platform reclaims from a post-stop hook, so a pod that goes away
    without a ``down`` leaves its HOME on either OS — and the report was gated to
    macOS, which is why the residue accumulated invisibly on Linux."""

    def _plane(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PodConfig:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        for n in ("orphan", "running"):
            (c.pod_root / n).mkdir(parents=True)
        (c.pod_root / ".e2e-artifacts").mkdir()  # dot dirs are not pods
        monkeypatch.setattr(rt, "active_names", lambda cc: {"running"})
        return c

    def test_reported_on_linux(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        c = self._plane(tmp_path, monkeypatch)
        assert rt.orphan_homes(c) == ["orphan"]

    def test_ls_surfaces_them_with_the_reclaim_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "health", lambda cfg, name, port, timeout=3: 200)
        pod_cli._ls(c, argparse.Namespace(json=False))
        out = capsys.readouterr().out
        assert "1 orphaned pod HOME(s)" in out
        assert "kirocrew pod down orphan" in out

    def test_the_json_shape_stays_live_pods_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Three callers parse this array; orphans are human-output only."""
        c = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "health", lambda cfg, name, port, timeout=3: 200)
        pod_cli._ls(c, argparse.Namespace(json=True))
        assert [r["name"] for r in json.loads(capsys.readouterr().out)] == ["running"]

    def test_ls_shows_each_orphans_age(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """An orphan left 5 minutes ago and one left 3 weeks ago need different
        handling; the HOME's mtime is the signal the report was missing."""
        c = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "health", lambda cfg, name, port, timeout=3: 200)
        three_days = time.time() - 3 * 86400
        os.utime(c.pod_root / "orphan", (three_days, three_days))
        pod_cli._ls(c, argparse.Namespace(json=False))
        assert "3d ago" in capsys.readouterr().out

    def test_an_unstattable_orphan_still_gets_its_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """A HOME that vanished between enumeration and the report must not
        crash the listing — age is a hint, never a gate, on the read path."""
        c = self._plane(tmp_path, monkeypatch)
        pod_cli._print_orphans(c, ["ghost"])
        out = capsys.readouterr().out
        assert "ghost" in out
        assert "age unknown" in out


class TestRelativeAge:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "0s ago"),
            (59, "59s ago"),
            (60, "1m ago"),
            (3600, "1h ago"),
            (86400 * 3 + 3600, "3d ago"),
            (-10, "0s ago"),  # future mtime (clock skew) clamps, never negative
        ],
    )
    def test_largest_whole_unit(self, seconds: float, expected: str) -> None:
        assert pod_cli._relative_age(seconds) == expected


class TestParseOlderThan:
    @pytest.mark.parametrize(
        "spec,expected",
        [("3d", 3 * 86400.0), ("12h", 12 * 3600.0), ("30m", 1800.0), ("45s", 45.0)],
    )
    def test_accepted_forms(self, spec: str, expected: float) -> None:
        assert pod_cli._parse_older_than(spec) == expected

    @pytest.mark.parametrize("spec", ["", "3", "d", "3w", "1.5h", "3d12h", "-3d", "9" * 400 + "d"])
    def test_rejects_anything_else(self, spec: str) -> None:
        with pytest.raises(rt.PodError, match="invalid --older-than"):
            pod_cli._parse_older_than(spec)

    def test_the_digit_cap_keeps_the_timestamp_arithmetic_finite(self) -> None:
        """An unbounded count (a 400-digit day value) survives int arithmetic
        but overflows the float timestamp subtraction in _prune with an
        uncaught OverflowError; the largest accepted count must stay finite."""
        biggest = pod_cli._parse_older_than("999999999d")
        assert time.time() - biggest == pytest.approx(time.time() - biggest)  # no OverflowError


class TestPrune:
    """`prune` is the N-at-once `down` for orphans: every delete routes through
    stop_pod (drain + verify), liveness is re-checked per name at delete time,
    and one bad name never aborts the sweep."""

    def _plane(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orphans: tuple[str, ...]
    ) -> PodConfig:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        c = PodConfig.load()
        for n in orphans:
            (c.pod_root / n).mkdir(parents=True)
        monkeypatch.setattr(rt, "require_backend", lambda: None)
        monkeypatch.setattr(rt, "active_names", lambda cc: set())
        monkeypatch.setattr(rt, "is_active", lambda cc, n: False)
        monkeypatch.setattr(rt, "unit_state", lambda cc, n: ("inactive", 0))
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        # cleanup_home directly from prune would BE the race stop_pod exists to
        # close — any call that does not come through stop_pod is a regression.
        monkeypatch.setattr(
            rt, "cleanup_home", lambda *a, **k: pytest.fail("prune bypassed stop_pod")
        )
        return c

    def _ns(self, **kw) -> argparse.Namespace:
        # prune_all=True keeps the reclaim-behavior tests on a full sweep; the
        # age-gate default is covered by its own dedicated tests below.
        base = {"older_than": "3d", "json": False, "dry_run": False, "prune_all": True}
        base.update(kw)
        return argparse.Namespace(**base)

    def test_reclaims_every_orphan_through_stop_pod(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c = self._plane(tmp_path, monkeypatch, ("a", "b"))
        stopped: list[str] = []
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: stopped.append(n) or _cp())
        rt.pin_checkout(c, "a", tmp_path / "co")
        pod_cli._prune(c, self._ns())
        out = capsys.readouterr().out
        assert stopped == ["a", "b"]
        assert not c.env_file("a").exists(), "the stale checkout pin must be cleared"
        assert "pruned: 2 reclaimed, 0 kept, 0 skipped, 0 failed" in out

    def test_older_than_keeps_the_young_orphan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c = self._plane(tmp_path, monkeypatch, ("old", "young"))
        five_days = time.time() - 5 * 86400
        os.utime(c.pod_root / "old", (five_days, five_days))
        stopped: list[str] = []
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: stopped.append(n) or _cp())
        pod_cli._prune(c, self._ns(older_than="3d", prune_all=False))
        out = capsys.readouterr().out
        assert stopped == ["old"]
        assert "1 reclaimed, 1 kept" in out

    def test_liveness_is_rechecked_at_delete_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """A name can go active between enumeration and its turn in the loop —
        deleting under a live unit is the one unrecoverable outcome."""
        c = self._plane(tmp_path, monkeypatch, ("woke-up",))
        monkeypatch.setattr(rt, "is_active", lambda cc, n: True)
        monkeypatch.setattr(
            rt, "stop_pod", lambda cc, n: pytest.fail("stopped a live pod during prune")
        )
        pod_cli._prune(c, self._ns())
        assert "pod is now active" in capsys.readouterr().out

    def test_one_failure_does_not_abort_the_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Per-name results: a prune where one of two succeeded must say which,
        keep going, and exit nonzero."""
        c = self._plane(tmp_path, monkeypatch, ("bad", "good"))
        monkeypatch.setattr(
            rt,
            "stop_pod",
            lambda cc, n: _cp(returncode=1, stderr="still writing") if n == "bad" else _cp(),
        )
        with pytest.raises(SystemExit) as exc:
            pod_cli._prune(c, self._ns())
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "still writing" in out
        assert "1 reclaimed" in out and "1 failed" in out

    def test_a_pod_error_on_one_name_is_contained(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c = self._plane(tmp_path, monkeypatch, ("erroring", "fine"))

        def _stop(cc: PodConfig, n: str) -> subprocess.CompletedProcess:
            if n == "erroring":
                raise rt.PodError("launchctl cannot answer")
            return _cp()

        monkeypatch.setattr(rt, "stop_pod", _stop)
        with pytest.raises(SystemExit):
            pod_cli._prune(c, self._ns())
        out = capsys.readouterr().out
        assert "launchctl cannot answer" in out
        assert "1 reclaimed" in out

    def test_a_name_reclaimed_by_a_new_pod_keeps_its_pin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """RECLAIMED_MARKER means the env file now pins the NEW pod's checkout."""
        c = self._plane(tmp_path, monkeypatch, ("handover",))
        rt.pin_checkout(c, "handover", tmp_path / "co")
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: _cp(stdout=rt.RECLAIMED_MARKER))
        pod_cli._prune(c, self._ns())
        assert c.env_file("handover").exists(), "a live pod's checkout pin must survive"
        assert "claimed by a new pod" in capsys.readouterr().out

    def test_macos_installed_plist_is_refused_at_delete_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The plist can be written by an `up` AFTER the orphan enumeration (which
        already excludes installed names) — the delete-time recheck is what
        stands between that pod and a bootout of its live service."""
        c = self._plane(tmp_path, monkeypatch, ("mid-up",))
        monkeypatch.setattr(rt, "IS_MACOS", True)
        plist = tmp_path / "mid-up.plist"
        plist.write_text("")
        monkeypatch.setattr(rt.launchd, "plist_path", lambda cc, n: plist)
        monkeypatch.setattr(
            rt, "stop_pod", lambda cc, n: pytest.fail("booted-out an installed pod")
        )
        res = pod_cli._prune_one(c, "mid-up")
        assert res["status"] == "skipped"
        assert "pod is now installed" in res["detail"]

    def test_json_shape_is_per_name_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c = self._plane(tmp_path, monkeypatch, ("a",))
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: _cp())
        pod_cli._prune(c, self._ns(json=True))
        rows = json.loads(capsys.readouterr().out)
        assert rows == [{"name": "a", "status": "reclaimed", "detail": ""}]

    def test_json_stays_valid_when_the_delete_path_prints(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """stop_pod/cleanup_home print diagnostics to stdout; interleaved with
        the machine output they would corrupt the JSON document. They must be
        rerouted to stderr, not swallowed."""
        c = self._plane(tmp_path, monkeypatch, ("noisy",))

        def _stop(cc: PodConfig, n: str) -> subprocess.CompletedProcess:
            print("pod cleanup did not fully remove /x: still present")
            return _cp(returncode=1, stderr="teardown incomplete")

        monkeypatch.setattr(rt, "stop_pod", _stop)
        with pytest.raises(SystemExit):
            pod_cli._prune(c, self._ns(json=True))
        captured = capsys.readouterr()
        rows = json.loads(captured.out)  # the whole stdout must parse
        assert rows[0]["status"] == "failed"
        assert "still present" in captured.err

    def test_a_refused_invocation_is_still_audited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bulk-destructive verb that exits on a refusal (dead backend, bad
        duration) must reach the audit trail — an unrecorded refused prune is
        invisible to the log."""
        c = self._plane(tmp_path, monkeypatch, ())
        events: list[tuple[str, str]] = []
        monkeypatch.setattr(
            pod_cli, "_audit", lambda op, outcome, res="", error="": events.append((op, outcome))
        )
        with pytest.raises(rt.PodError):
            pod_cli._prune(c, self._ns(older_than="banana", prune_all=False))
        assert ("pod.prune", "denied") in events

    def test_a_failed_enumeration_is_audited_before_the_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._plane(tmp_path, monkeypatch, ())
        events: list[str] = []
        monkeypatch.setattr(
            pod_cli, "_audit", lambda op, outcome, res="", error="": events.append(outcome)
        )

        def _boom(cc: PodConfig) -> list[str]:
            raise rt.PodError("launchctl cannot enumerate")

        monkeypatch.setattr(rt, "orphan_homes", _boom)
        with pytest.raises(rt.PodError):
            pod_cli._prune(c, self._ns())
        assert "denied" in events

    def test_every_prune_one_path_emits_exactly_one_audit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The audit is the single exit chokepoint of _prune_one: no decision
        path — including the invalid-name refusal — may return without a SEL
        event, and none may double-audit."""
        c = self._plane(tmp_path, monkeypatch, ("plain",))
        events: list[tuple[str, str]] = []
        monkeypatch.setattr(
            pod_cli, "_audit", lambda op, outcome, res="", error="": events.append((outcome, res))
        )
        scenarios: list[tuple[str, dict, str]] = [
            ("invalid name", {}, "denied"),
            ("active", {"is_active": lambda cc, n: True}, "denied"),
            ("mid-restart", {"unit_state": lambda cc, n: ("activating", 1)}, "denied"),
            ("stop fails", {"stop_pod": lambda cc, n: _cp(returncode=1, stderr="x")}, "failure"),
            ("reclaimed", {"stop_pod": lambda cc, n: _cp()}, "allowed"),
            (
                "handover",
                {"stop_pod": lambda cc, n: _cp(stdout=rt.RECLAIMED_MARKER)},
                "allowed",
            ),
        ]
        for label, patches, expected in scenarios:
            # Re-pin the baseline before layering the scenario's patch, so a
            # previous scenario's monkeypatch cannot leak forward.
            monkeypatch.setattr(rt, "is_active", lambda cc, n: False)
            monkeypatch.setattr(rt, "unit_state", lambda cc, n: ("inactive", 0))
            for attr, fn in patches.items():
                monkeypatch.setattr(rt, attr, fn)
            events.clear()
            name = "not a pod" if label == "invalid name" else "plain"
            pod_cli._prune_one(c, name)
            assert len(events) == 1, f"{label}: expected exactly one audit, got {events}"
            assert events[0][0] == expected, f"{label}: outcome {events[0][0]} != {expected}"

    def test_nothing_to_prune_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c = self._plane(tmp_path, monkeypatch, ())
        pod_cli._prune(c, self._ns())
        assert "no orphaned pod HOMEs to prune" in capsys.readouterr().out

    def test_prune_is_dispatchable(self) -> None:
        assert pod_cli._VERBS["prune"] is pod_cli._prune

    def test_a_bare_prune_keeps_a_fresh_crash_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """The CLI default is --older-than 3d: a bare `pod prune` must not
        sweep the minutes-old crash HOME an operator is still debugging — its
        logs are the only postmortem evidence. --all is the explicit opt-in."""
        c = self._plane(tmp_path, monkeypatch, ("fresh",))
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: pytest.fail("swept a fresh crash"))
        pod_cli._prune(c, self._ns(prune_all=False))  # CLI defaults
        assert "kept" in capsys.readouterr().out

    def test_a_restarting_unit_is_refused_at_delete_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """During the Restart=on-failure backoff the unit is `activating` — not
        active — so both the orphan scan and is_active miss it, and a stop here
        would cancel the pending restart and delete a running pod's HOME."""
        c = self._plane(tmp_path, monkeypatch, ("mid-backoff",))
        monkeypatch.setattr(rt, "unit_state", lambda cc, n: ("activating", 1))
        monkeypatch.setattr(
            rt, "stop_pod", lambda cc, n: pytest.fail("cancelled a pending restart")
        )
        pod_cli._prune(c, self._ns())
        assert "mid-transition or restarting" in capsys.readouterr().out

    def test_a_crash_looping_unit_is_refused_even_when_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """restarts>0 means the service manager is still involved with this
        name — fail closed and leave it to an explicit `pod down`."""
        c = self._plane(tmp_path, monkeypatch, ("crashy",))
        monkeypatch.setattr(rt, "unit_state", lambda cc, n: ("failed", 2))
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: pytest.fail("reclaimed a managed unit"))
        pod_cli._prune(c, self._ns())
        assert "not orphaned" in capsys.readouterr().out

    def test_an_unknown_unit_state_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c = self._plane(tmp_path, monkeypatch, ("murky",))
        monkeypatch.setattr(rt, "unit_state", lambda cc, n: ("unknown", 0))
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: pytest.fail("deleted on no evidence"))
        pod_cli._prune(c, self._ns())
        assert "skipped" in capsys.readouterr().out

    def test_an_os_error_on_one_name_is_contained(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Raw filesystem/subprocess failures (not just PodError) must become a
        per-name failed row — an escaped exception would hide every result
        already earned and strand the remaining orphans unprocessed."""
        c = self._plane(tmp_path, monkeypatch, ("cursed", "fine"))

        def _stop(cc: PodConfig, n: str) -> subprocess.CompletedProcess:
            if n == "cursed":
                raise PermissionError("env dir is read-only")
            return _cp()

        monkeypatch.setattr(rt, "stop_pod", _stop)
        with pytest.raises(SystemExit):
            pod_cli._prune(c, self._ns())
        out = capsys.readouterr().out
        assert "env dir is read-only" in out
        assert "1 reclaimed" in out

    def test_dry_run_classifies_without_deleting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c = self._plane(tmp_path, monkeypatch, ("a", "b"))
        (c.pod_root / "not a pod").mkdir()  # invalid name: same verdict as a real run
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: pytest.fail("dry run deleted"))
        pod_cli._prune(c, self._ns(dry_run=True))
        out = capsys.readouterr().out
        assert "would-reclaim" in out
        assert "not a valid pod name" in out, "preview must match the real run's classification"
        assert "dry run: 2 would be reclaimed" in out
        assert (c.pod_root / "a").exists() and (c.pod_root / "b").exists()

    def test_a_stray_non_pod_directory_is_skipped_by_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """A directory that fails validate_name can never have a unit and can
        never be reclaimed by `down`; shelling a bogus systemctl stop for it
        would report failed and pin every future prune's exit at 1."""
        c = self._plane(tmp_path, monkeypatch, ())
        (c.pod_root / "not a pod").mkdir(parents=True)
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: pytest.fail("stopped a bogus unit"))
        pod_cli._prune(c, self._ns())
        assert "not a valid pod name" in capsys.readouterr().out

    def test_backend_absent_is_one_refusal_not_n_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a usable service manager the sweep must refuse once (the
        documented one-line `pod:` error), not report N stuck HOMEs."""
        c = self._plane(tmp_path, monkeypatch, ("a", "b"))

        def _absent() -> None:
            raise rt.PodBackendAbsent("no session bus")

        monkeypatch.setattr(rt, "require_backend", _absent)
        with pytest.raises(rt.PodError):
            pod_cli._prune(c, self._ns())

    def test_older_than_measures_activity_not_creation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """A pod created 5 days ago that crashed 10 minutes ago is the orphan
        being debugged — its fresh log write must keep it, even though the HOME
        directory's own mtime froze at creation."""
        c = self._plane(tmp_path, monkeypatch, ("fresh-crash",))
        home = c.pod_root / "fresh-crash"
        (home / "logs").mkdir()
        (home / "logs" / "gateway.log").write_text("boom")  # fresh mtime (now)
        five_days = time.time() - 5 * 86400
        os.utime(home / "logs", (five_days, five_days))
        os.utime(home, (five_days, five_days))
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: pytest.fail("reclaimed a fresh crash"))
        pod_cli._prune(c, self._ns(older_than="3d", prune_all=False))
        assert "kept" in capsys.readouterr().out


class TestOrphanSymlinkSafety:
    """A symlink under pod_root can point at a LIVE pod's HOME (or anywhere);
    following it — in the enumeration or at the delete chokepoint — turns a
    bulk reclaim into deleting a directory that was never an orphan."""

    def test_orphan_homes_never_lists_a_symlink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        (c.pod_root / "live").mkdir(parents=True)
        (c.pod_root / "alias").symlink_to(c.pod_root / "live")
        monkeypatch.setattr(rt, "active_names", lambda cc: {"live"})
        assert rt.orphan_homes(c) == []

    def test_cleanup_home_refuses_to_follow_a_symlink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        victim = c.pod_root / "live"
        victim.mkdir(parents=True)
        (victim / "sessions.db").write_text("precious")
        (c.pod_root / "alias").symlink_to(victim)
        assert rt.cleanup_home(c, "alias") == 2
        assert (victim / "sessions.db").exists(), "followed the link and deleted the target"
        assert "symlink" in capsys.readouterr().out

    def test_cleanup_home_deletes_by_name_never_by_resolved_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The symlink pre-check can be raced (entry swapped to a symlink
        between check and delete). What makes that race harmless is that
        rmtree receives the UNRESOLVED name — stdlib rmtree refuses a
        top-level symlink — never the resolved path, which at swap time is
        the live sibling the link points at. Pin the argument."""
        real = tmp_path / "real-pods"
        real.mkdir()
        (tmp_path / "pods").symlink_to(real)  # unresolved != resolved spelling
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        (c.pod_root / "demo").mkdir()
        seen: list[Path] = []
        monkeypatch.setattr(
            rt.shutil, "rmtree", lambda p, ignore_errors=False: seen.append(Path(p))
        )
        rt.cleanup_home(c, "demo")
        assert seen == [c.pod_root / "demo"], "rmtree must get the unresolved name"
        assert seen[0] != (c.pod_root / "demo").resolve(), "test setup lost the distinction"

    def test_a_dangling_symlink_swap_is_reported_not_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """rmtree refuses a symlink SILENTLY under ignore_errors, and a
        DANGLING link's resolved target does not exist — so verifying by
        target existence would report a clean reclaim while the link remains
        as residue the orphan scan (which skips symlinks) can never surface
        again. The verification must be lexists on the entry itself."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        entry = c.pod_root / "swapped"
        entry.mkdir(parents=True)  # a real dir at pre-check time

        def _swap_then_refuse(p, ignore_errors=False):
            # Net effect of the worst-case interleaving: by the time rmtree
            # runs, the entry is a dangling symlink, which rmtree refuses
            # (silently, under ignore_errors).
            entry.rmdir()
            entry.symlink_to(c.pod_root / "gone")

        monkeypatch.setattr(rt.shutil, "rmtree", _swap_then_refuse)
        rc = rt.cleanup_home(c, "swapped")
        assert rc == 1, "a surviving entry must be a reported failure, never rc 0"
        assert "symlink" in capsys.readouterr().out


class TestDownSamplesStateUnderTheLock:
    """`was_up` / `had_home` decide whether a failed stop is fatal, so sampling
    them before taking the lock let a concurrent `up` invalidate the answer: we
    saw "not running, nothing to reclaim", waited on the lock, and then judged a
    REAL failure against that stale reading — swallowing it and deleting the live
    pod's checkout pin."""

    def test_state_is_read_after_the_lock_is_held(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import contextlib as _ctx

        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        c = PodConfig.load()
        rt.pin_checkout(c, "demo", tmp_path / "co")
        order: list[str] = []

        @_ctx.contextmanager
        def _mutex(cc: PodConfig, n: str):
            order.append("lock")
            # What a concurrent `up` completes while we are blocked on the lock.
            c.home_dir(n).mkdir(parents=True, exist_ok=True)
            yield

        def _is_active(cc: PodConfig, n: str) -> bool:
            order.append("sample")
            return False

        monkeypatch.setattr(rt, "pod_name_mutex", _mutex)
        monkeypatch.setattr(rt, "is_active", _is_active)
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: _cp(returncode=1, stderr="boom"))
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)

        # The failure must be fatal, judged against state read INSIDE the lock.
        with pytest.raises(SystemExit):
            pod_cli._down(c, argparse.Namespace(name="demo"))
        assert order == ["lock", "sample"], "state sampled before the lock is stale"
        assert c.env_file("demo").exists(), "a live pod's checkout pin must survive"


@requires_posix_pod_lifecycle
class TestDownReclaimsResidue:
    """``pod down`` is the reclaim command the orphan report points at, so it has
    to work on a pod that is no longer running."""

    def _orphan(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[PodConfig, Path]:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / "config.json").write_text("{}")
        rt.pin_checkout(c, "demo", tmp_path / "co")
        monkeypatch.setattr(rt, "is_active", lambda cc, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp())
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        return c, home

    def test_down_reclaims_a_home_left_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c, home = self._orphan(tmp_path, monkeypatch)
        pod_cli._down(c, argparse.Namespace(name="demo"))
        assert not home.exists()
        assert not c.env_file("demo").exists()
        out = capsys.readouterr().out
        assert "reclaimed the isolated HOME" in out
        assert "nothing to stop" not in out

    def test_down_on_a_never_used_name_is_still_a_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """`systemctl stop` on an instance of a template that was never installed
        reports "unit not loaded" (rc 5). With no HOME to reclaim and the pod not
        running there is nothing at stake, so `pod down <name>` must stay the
        documented no-op rather than exiting 1."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        c = PodConfig.load()
        monkeypatch.setattr(rt, "is_active", lambda cc, n: False)
        monkeypatch.setattr(
            rt,
            "systemctl",
            lambda *a, **k: _cp(returncode=5, stderr="Unit kirocrew-pod@demo.service not loaded."),
        )
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        pod_cli._down(c, argparse.Namespace(name="demo"))  # must not SystemExit
        assert "nothing to stop" in capsys.readouterr().out

    def test_down_still_fails_loudly_when_a_home_is_there_to_reclaim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mirror of the no-op above: the same failed stop, but now there IS
        residue, so swallowing it would be the silent success being fixed here."""
        c, home = self._orphan(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=5, stderr="nope"))
        with pytest.raises(SystemExit):
            pod_cli._down(c, argparse.Namespace(name="demo"))
        assert home.exists()
        assert c.env_file("demo").exists()

    def test_down_fails_loudly_when_the_reclaim_could_not_finish(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A silent success here is the whole bug: the operator is told zero
        residue while the directory (and its checkout pin) are still there."""
        c, home = self._orphan(tmp_path, monkeypatch)
        monkeypatch.setattr(rt.shutil, "rmtree", lambda *a, **k: None)
        with pytest.raises(SystemExit):
            pod_cli._down(c, argparse.Namespace(name="demo"))
        assert home.exists()
        assert c.env_file("demo").exists(), "the pin must survive a failed teardown"


class TestHostStateIsFenced:
    """A pod test must not be able to write the machine's own systemd unit.

    Found on a real host: a suite run rewrote `~/.config/systemd/user/
    kirocrew-pod@.service` with a test's tmpdir as
    `Environment=KIROCREW_POD_ROOT=`, after which every `pod up` died with "no
    pinned checkout" until someone re-ran `pod install`. `unit_path` reads
    `Path.home()`, which no test patched, and `_up` -> `install_backend` ->
    `install_unit` reaches it. Asserting the PATH rather than the write keeps this
    guard from having to clobber the real unit to prove itself.
    """

    def test_the_home_is_pinned_away_from_the_real_one(self) -> None:
        """The fixture must actually redirect. Asserted separately from the paths
        below because on Windows a pytest tmp dir lives UNDER `Path.home()`
        (`C:/Users/<u>/AppData/Local/Temp/...`), so "not under the real home" is
        false there by construction and cannot carry the guard."""
        assert Path.home() != _REAL_HOME

    def test_the_unit_path_follows_the_pinned_home(self, cfg: PodConfig) -> None:
        assert Path.home() in unit_mod.unit_path(cfg).parents

    def test_pod_host_dirs_follow_the_pinned_home(self, cfg: PodConfig) -> None:
        # pods_dir carries the lifecycle lock file; pod_root carries pod HOMEs.
        for path in (cfg.pods_dir, cfg.pod_root):
            assert Path.home() in path.parents, path


class TestRuntimeHelpers:
    def test_is_active(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        assert rt.is_active(cfg, "x") is True
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=3))
        assert rt.is_active(cfg, "x") is False

    def test_active_names_parses_units(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = (
            "kirocrew-pod@alpha.service    loaded active running x\n"
            "kirocrew-pod@beta-two.service loaded active running y\n"
            "unrelated.service             loaded active running z\n"
        )
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(stdout=out))
        assert rt.active_names(cfg) == {"alpha", "beta-two"}

    def test_unit_state(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            rt, "systemctl", lambda *a, **k: _cp(stdout="ActiveState=failed\nNRestarts=2\n")
        )
        assert rt.unit_state(cfg, "x") == ("failed", 2)

    def test_unit_state_defaults(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(stdout="garbage\n"))
        assert rt.unit_state(cfg, "x") == ("unknown", 0)

    def test_health_codes(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error

        class _Resp:
            status = 200

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

        # Ownership is proven separately (see TestPortOwner); here the pod owns
        # its port so health reports the raw code.
        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_POD)
        monkeypatch.setattr(rt, "loopback_urlopen", lambda *a, **k: _Resp())
        assert rt.health(cfg, "demo", 7999) == 200

        def _raise_http(*a: object, **k: object) -> None:
            raise urllib.error.HTTPError("u", 403, "f", {}, None)  # type: ignore[arg-type]

        monkeypatch.setattr(rt, "loopback_urlopen", _raise_http)
        assert rt.health(cfg, "demo", 7999) == 403

        def _raise_url(*a: object, **k: object) -> None:
            raise urllib.error.URLError("down")

        monkeypatch.setattr(rt, "loopback_urlopen", _raise_url)
        assert rt.health(cfg, "demo", 7999) == 0

    def test_health_treats_a_non_http_listener_as_unreachable(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A process holding the port that does not speak HTTP is not "serving".

        ``BadStatusLine`` is neither ``OSError`` nor ``URLError``, so without an
        explicit catch it escapes as a traceback out of ``pod status`` — and a
        foreign listener on a derived port is exactly when it happens.
        """
        import http.client

        def _raise_bad_status(*a: object, **k: object) -> None:
            raise http.client.BadStatusLine("garbage")

        monkeypatch.setattr(rt, "loopback_urlopen", _raise_bad_status)
        assert rt.health(cfg, "demo", 7999) == 0

    def test_health_reports_a_foreign_responder_as_not_up(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bug: a 200 from somebody else's gateway must not read as up."""

        class _Resp:
            status = 200

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

        monkeypatch.setattr(rt, "loopback_urlopen", lambda *a, **k: _Resp())
        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_FOREIGN)
        assert rt.health(cfg, "demo", 7999) == rt.HEALTH_FOREIGN

    def test_health_keeps_the_code_when_ownership_is_unprovable(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No listener-lookup tool must not turn every pod into an unhealthy one."""

        class _Resp:
            status = 200

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

        monkeypatch.setattr(rt, "loopback_urlopen", lambda *a, **k: _Resp())
        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_UNPROVEN)
        assert rt.health(cfg, "demo", 7999) == 200

    def test_health_skips_the_ownership_check_when_nothing_answers(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stopped pod costs one refused connection and no process lookup."""
        import urllib.error

        def _raise_url(*a: object, **k: object) -> None:
            raise urllib.error.URLError("down")

        calls: list[object] = []

        def _owner(*a: object, **k: object) -> str:
            calls.append(a)
            return rt.OWNER_FOREIGN

        monkeypatch.setattr(rt, "loopback_urlopen", _raise_url)
        monkeypatch.setattr(rt, "port_owner", _owner)
        assert rt.health(cfg, "demo", 7999) == 0
        assert calls == []

    def test_mint_token_reads_secret_and_posts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / ".local_secret").write_text("s3cret")

        class _Resp:
            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"token":"tok-xyz"}'

        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_POD)
        monkeypatch.setattr(rt, "loopback_urlopen", lambda *a, **k: _Resp())
        assert rt.mint_token(c, "demo", "1h") == "tok-xyz"

    def test_mint_token_refuses_a_foreign_port_holder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never hand this pod's secret to whatever happens to answer.

        Sharper than the health misreport: mint sends the pod's own
        ``.local_secret`` and returns a dashboard token for the responder, so
        against a squatter it prints a URL that drives the wrong instance.
        """
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / ".local_secret").write_text("s3cret")

        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_FOREIGN)
        monkeypatch.setattr(
            rt, "loopback_urlopen", lambda *a, **k: pytest.fail("must not dial a foreign listener")
        )
        with pytest.raises(rt.PodError, match="another process"):
            rt.mint_token(c, "demo", "1h")

    def test_mint_token_refuses_when_ownership_is_merely_unproven(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The credential path needs POSITIVE proof, unlike the health readout.

        The two ways of being wrong are not symmetrical. Refusing a live pod costs
        an error message; proceeding on an unproven port hands a different local
        user -- who can bind 127.0.0.1 but cannot read this 0600 secret -- a
        credential for this pod. ``_gateway_owns_port`` fails closed on the same
        path for the same reason, including when the listener lookup is missing.
        """
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / ".local_secret").write_text("s3cret")

        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_UNPROVEN)
        monkeypatch.setattr(
            rt,
            "loopback_urlopen",
            lambda *a, **k: pytest.fail("must not dial an unproven listener"),
        )
        with pytest.raises(rt.PodOwnershipUnproven, match="could not prove"):
            rt.mint_token(c, "demo", "1h")

    def test_the_unprovable_refusal_is_a_distinguishable_type(self) -> None:
        """`pod up` treats the two refusals differently, so they must be tellable.

        FOREIGN is positive knowledge that the port is somebody else's; UNPROVEN is
        the absence of knowledge. Both withhold the secret, but only the first
        means there is nothing truthful left for `pod up` to print.
        """
        assert issubclass(rt.PodOwnershipUnproven, rt.PodError)
        # A FOREIGN refusal must NOT be catchable as the unprovable one.
        assert not issubclass(rt.PodError, rt.PodOwnershipUnproven)

    def test_mint_token_no_secret_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        with pytest.raises(rt.PodError):
            rt.mint_token(PodConfig.load(), "ghost", "1h")


class TestAuditEvents:
    @requires_posix_pod_lifecycle
    def test_down_emits_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[tuple] = []
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: recorded.append((a, k)))
        monkeypatch.setattr(rt, "is_active", lambda cfg, name: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        pod_cli._down(PodConfig.load(), argparse.Namespace(name="demo"))
        assert any("pod.down" in str(r) for r in recorded)

    @requires_posix_pod_lifecycle
    def test_down_dies_when_stop_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[tuple] = []
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: recorded.append((a, k)))
        monkeypatch.setattr(rt, "is_active", lambda cfg, name: True)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=1, stderr="bus error"))
        with pytest.raises(SystemExit):
            pod_cli._down(PodConfig.load(), argparse.Namespace(name="demo"))
        assert any("failure" in str(r) for r in recorded)

    def test_token_emits_audit_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[tuple] = []
        monkeypatch.setattr(
            pod_cli, "_audit", lambda op, outcome, *a, **k: recorded.append((op, outcome))
        )
        monkeypatch.setattr(rt, "mint_token", lambda cfg, name, ttl: "tok-abc")
        pod_cli._token(PodConfig.load(), argparse.Namespace(name="demo", ttl="2h"))
        assert ("pod.token", "allowed") in recorded

    def test_audit_failure_is_logged_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        def _boom(*a: object, **k: object) -> None:
            raise RuntimeError("sel down")

        monkeypatch.setattr(pod_cli, "sel", lambda: type("S", (), {"log_api_access": _boom})())
        with caplog.at_level(logging.WARNING):
            pod_cli._audit("pod.up", "allowed", "name=x")  # must NOT raise
        assert any("SEL audit failed for pod.up" in r.message for r in caplog.records)


class TestCliVerbs:
    def test_ls_empty(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.setattr(rt, "active_names", lambda c: set())
        pod_cli._ls(cfg, argparse.Namespace(json=False))
        assert "no pods running" in capsys.readouterr().out

    def test_ls_table_and_json(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setattr(rt, "active_names", lambda c: {"alpha"})
        monkeypatch.setattr(rt, "derive_port", lambda c, n: 7811)
        monkeypatch.setattr(rt, "health", lambda cfg, name, p: 403)
        pod_cli._ls(cfg, argparse.Namespace(json=False))
        assert "alpha" in capsys.readouterr().out
        pod_cli._ls(cfg, argparse.Namespace(json=True))
        out = capsys.readouterr().out
        assert '"port": 7811' in out and '"health": 403' in out

    def test_status(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.setattr(rt, "derive_port", lambda c, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda c, n: True)
        monkeypatch.setattr(rt, "health", lambda cfg, name, p: 403)
        pod_cli._status(cfg, argparse.Namespace(name="alpha", json=True))
        out = capsys.readouterr().out
        assert '"status": "up"' in out and '"health": 403' in out

    def test_url(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.setattr(rt, "derive_port", lambda c, n: 7811)
        pod_cli._url(cfg, argparse.Namespace(name="alpha"))
        assert "http://127.0.0.1:7811" in capsys.readouterr().out

    @requires_posix_pod_lifecycle
    def test_install_writes_unit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        # This test asserts the LINUX path, so satisfy the platform gate — the
        # suite must exercise it on macOS/Windows runners too.
        monkeypatch.setattr(rt, "require_systemd", lambda: None)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        recorded: list[tuple] = []
        monkeypatch.setattr(
            pod_cli, "_audit", lambda op, outcome, *a, **k: recorded.append((op, outcome))
        )
        c = PodConfig.load()
        pod_cli._install(c, argparse.Namespace())
        dst = tmp_path / ".config" / "systemd" / "user" / f"{c.unit_prefix}@.service"
        assert dst.exists() and "pod _run %i" in dst.read_text(encoding="utf-8")
        assert "daemon-reload OK" in capsys.readouterr().out
        assert ("pod.install", "allowed") in recorded

    @requires_posix_pod_lifecycle
    def test_install_dies_on_reload_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(rt, "require_systemd", lambda: None)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=1))
        recorded: list[tuple] = []
        monkeypatch.setattr(
            pod_cli, "_audit", lambda op, outcome, *a, **k: recorded.append((op, outcome))
        )
        with pytest.raises(SystemExit):
            pod_cli._install(PodConfig.load(), argparse.Namespace())
        assert ("pod.install", "failure") in recorded

    def test_dispatch_unknown_verb_exits(self) -> None:
        with pytest.raises(SystemExit):
            pod_cli.dispatch(argparse.Namespace(pod_action=None))


@requires_posix_pod_lifecycle
class TestUpVerb:
    """Drive the big _up body end-to-end with the host boundary mocked."""

    def _prep(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        ready: bool = True,
        dist: bool = True,
    ) -> PodConfig:
        monkeypatch.setenv("KIROCREW_POD_WORKTREES_ROOT", str(tmp_path / "wts"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        # Force git resolution to miss so the root fallback resolves deterministically.
        monkeypatch.setattr(rt, "_git_worktrees", lambda ref: {})
        # Insulate `_up` from the HOST's real port occupancy. `allocate_port`
        # bind-probes, so without this a developer's own pod sitting on the
        # derived port would push these tests down the fallback path and they
        # would disagree with the port they assert -- a failure that depends on
        # who is running them. Tests about the fallback override this.
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: True)
        if ready:
            _ready_worktree(tmp_path / "wts", "demo", venv=True, dist=dist)
        return PodConfig.load()

    def test_up_happy_path_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        c = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )
        out = capsys.readouterr().out
        assert '"port": 7811' in out and '"token": "tok-9"' in out
        # _up must pin the resolved checkout for the systemd boot.
        assert rt.read_env_file(c, "demo").get("CHECKOUT", "").endswith("wts/demo")

    def test_up_with_a_seed_runs_the_landing_verification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        calls: list[tuple[str, str, bool]] = []
        monkeypatch.setattr(
            pod_cli,
            "_verify_seed_landed",
            lambda cfg, name, scenario, home_was_populated: calls.append(
                (name, scenario, home_was_populated)
            ),
        )

        pod_cli._up(
            c,
            argparse.Namespace(name="demo", json=True, seed="minimal", ttl="2h", provision=False),
        )

        assert calls == [("demo", "minimal", False)]

    @pytest.mark.parametrize("landed", ["", "minimal"])
    def test_populated_home_seed_request_refuses_before_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, landed: str
    ) -> None:
        """A failed start cleans up the pod home, so verification after health is
        too late for state that existed before this command. Every seed request
        against a populated home must refuse before `start_pod` can reach
        `stop_pod`, including one whose marker already matches the request."""
        c = self._prep(tmp_path, monkeypatch)
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / "sessions").mkdir()
        starts: list[str] = []
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(
            rt,
            "start_pod",
            lambda cfg, name: (starts.append(name) or _cp(returncode=0)),
        )
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        # This wiring test owns the pre-start decision, not POSIX marker I/O.
        monkeypatch.setattr(rt, "seeded_scenario_in_home", lambda cfg, name: landed)

        with pytest.raises(SystemExit) as excinfo:
            pod_cli._up(
                c,
                argparse.Namespace(
                    name="demo", json=True, seed="minimal", ttl="2h", provision=False
                ),
            )

        assert excinfo.value.code == 1
        assert starts == []
        assert (home / "sessions").is_dir()

    def test_up_still_succeeds_when_ownership_cannot_be_proven(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """An lsof-less host must not boot the pod and then fail the command.

        The credential is the last step of `up`, not its point. Dying here would
        make `pod up` impossible on a POSIX host with no listener-lookup tool,
        where ownership can never be proven -- a self-inflicted outage in place of
        a hardened credential path. The secret still never reaches the wire: the
        guard is inside mint_token, which raised before dialling.
        """
        c = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p: 403)

        def _unprovable(cfg: object, n: object, ttl: object) -> str:
            raise rt.PodOwnershipUnproven("could not prove which process holds :7811")

        monkeypatch.setattr(rt, "mint_token", _unprovable)
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )
        cap = capsys.readouterr()
        out = cap.out
        # Reports what it knows, withholds only the credential.
        assert '"status": "up"' in out and '"port": 7811' in out
        assert '"base_url": "http://127.0.0.1:7811"' in out
        # The empty token IS the machine signal; the JSON shape gains no field for
        # it, so the reason travels on stderr where a caller and a human both see
        # it. A payload key with no consumer would be schema committed forever.
        assert '"token": ""' in out
        assert "token_withheld" not in out
        assert "could not prove" in cap.err

    def test_up_still_dies_on_a_foreign_port_holder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FOREIGN is positive knowledge, so there is nothing truthful left to print."""
        c = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p: 403)

        def _foreign(cfg: object, n: object, ttl: object) -> str:
            raise rt.PodError("held by another process")

        monkeypatch.setattr(rt, "mint_token", _foreign)
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        with pytest.raises(SystemExit):
            pod_cli._up(
                c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
            )

    def test_up_refuses_live_port(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        c = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: c.live_port)
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        with pytest.raises(SystemExit):
            pod_cli._up(
                c, argparse.Namespace(name="demo", json=False, seed="", ttl="2h", provision=False)
            )

    def test_up_pins_the_fallback_so_every_reader_agrees(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """The defect end to end: a busy derived port must not be booted onto.

        The pin is the load-bearing part. `pod url`, `pod ls`, `pod exec` and Dev
        Fleet each call `derive_port` independently, so without recording the move
        they would keep printing the derived port while the gateway listens
        somewhere else -- which is what presented as "a healthy pod that is not the
        one I just built".
        """
        c = self._prep(tmp_path, monkeypatch)
        derived = rt.derive_port(c, "demo")
        monkeypatch.setattr(rt, "_port_is_free", lambda p: p != derived)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)

        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )

        captured = capsys.readouterr()
        pinned = rt.read_env_file(c, "demo").get("PORT")
        assert pinned and int(pinned) != derived, "the move must be recorded as a PORT= pin"
        # The whole point of pinning: the readers now agree with the boot.
        assert rt.derive_port(c, "demo") == int(pinned)
        assert f'"port": {int(pinned)}' in captured.out, "the reported port must be the real one"
        assert "already in use" in captured.err, "a silent move is the defect, not the fix"

    def test_up_does_not_move_a_running_pods_port(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An `up` against an ALREADY-ACTIVE pod must not reallocate.

        That pod's port is busy precisely because it owns it, so probing would
        "discover" a collision and move a running pod out from under every reader.
        The active check therefore gates the allocation, not just the start.
        """
        c = self._prep(tmp_path, monkeypatch)
        # Nothing is free: if allocation ran at all it would raise or relocate.
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: False)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: True)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        monkeypatch.setattr(
            rt, "start_pod", lambda *a, **k: pytest.fail("an active pod must not be restarted")
        )

        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )

        assert "PORT" not in rt.read_env_file(
            c, "demo"
        ), "a running pod's port must be left exactly as its readers already resolve it"

    def test_up_clears_a_stale_auto_marker_when_the_operator_takes_over(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A marker must not outlive the pin it described.

        The trap is a natural user action, not a coincidence: the pod is auto-moved
        to a port, the operator sees that and pins THAT port by hand. If the old
        marker survives, their deliberate pin now matches it, reads as machine-made,
        and may be relocated out from under them -- the exact guarantee this PR
        exists to give them.

        So the marker is cleared the moment an operator pin is detected, and the
        follow-on is asserted too: pinning the previously-auto value is respected.
        """
        c = self._prep(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        # State after an earlier automatic allocation to 7850, then the operator
        # editing PORT to something else.
        c.env_file("demo").write_text(f"PORT='7999'\n{rt.AUTO_PORT_KEY}='7850'\n")
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: True)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)

        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )

        env = rt.read_env_file(c, "demo")
        assert env.get("PORT") == "7999", "the operator's pin must be honoured"
        assert not env.get(rt.AUTO_PORT_KEY), (
            f"a stale marker survived as {env.get(rt.AUTO_PORT_KEY)!r}; if the "
            "operator later pins that same port it would read as machine-made"
        )
        # The follow-on: pinning the previously-auto port is now respected.
        c.env_file("demo").write_text(f"PORT='7850'\n{rt.AUTO_PORT_KEY}=''\n")
        assert rt.operator_pinned(c, "demo") is True, (
            "after the marker is cleared, pinning the old auto port must read as "
            "the operator's deliberate choice"
        )

    def test_up_never_marks_an_operator_pin_as_automatic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Recording the claim must not launder a deliberate pin into a movable one.

        `PORT_AUTO` is what licenses a later relocation. Since the claim is now
        recorded on EVERY allocation, an operator's own `PORT=` passes through that
        write -- and stamping the marker on it would quietly convert the one pin this
        code promises never to move into one it may move on the next contention.
        """
        c = self._prep(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        c.env_file("demo").write_text("PORT='7999'\n")  # a person set this, no marker
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: True)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)

        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )

        env = rt.read_env_file(c, "demo")
        assert env.get("PORT") == "7999", "the operator's pin must be honoured"
        assert not env.get(rt.AUTO_PORT_KEY), (
            "the operator's pin was marked automatic, so a later collision would "
            "silently relocate a port they deliberately chose. (An EMPTY marker is "
            "no marker -- the key may be present and blank, since write_env_file "
            "merges and cannot delete.)"
        )
        assert rt.operator_pinned(c, "demo") is True, "it must still read as theirs"

    def test_up_records_the_claim_even_when_nothing_moved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The claim must not be conditional on having been displaced.

        This is the ruling's whole point. A unit is `Type=simple`, so `start_pod`
        returns before the gateway binds; until it does, a bind probe reports this
        pod's port FREE. If an undisplaced pod records nothing, a concurrently
        starting colliding name is handed the same port and one gateway crash-loops
        -- the original defect arriving through the concurrent door.

        So the recording is asserted for the case where NOTHING moved, which is the
        case a displacement-only test cannot see.
        """
        c = self._prep(tmp_path, monkeypatch)
        derived = rt.derive_port(c, "demo")
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: True)  # nothing is busy
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)

        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )

        env = rt.read_env_file(c, "demo")
        assert env.get("PORT") == str(derived), (
            "an undisplaced pod must still record its claim -- otherwise it is "
            "invisible to a colliding name until its gateway binds"
        )
        assert env.get(rt.AUTO_PORT_KEY) == str(derived), "the claim must be marked ours"

    def test_up_records_the_auto_marker_beside_the_pin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Without the marker the fallback is indistinguishable from a deliberate
        # operator pin, and the pod can never be relocated again.
        c = self._prep(tmp_path, monkeypatch)
        derived = rt.derive_port(c, "demo")
        monkeypatch.setattr(rt, "_port_is_free", lambda p: p != derived)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )
        env = rt.read_env_file(c, "demo")
        assert env["PORT"] == env[rt.AUTO_PORT_KEY], "the marker must record the port WE chose"

    def test_up_refreshes_a_stale_port_when_the_pod_is_already_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The concurrent same-name case, which must not tear down a healthy pod.

        `port` is first read BEFORE the mutex. If a concurrent `up` wins the lock,
        allocates a fallback and pins it, this call wakes holding the OLD derived
        port -- and the health wait stops the unit when it cannot reach `port`, so a
        stale value would stop the pod the other `up` just started successfully.

        The pin therefore has to appear DURING the lock wait, not before it: with it
        already on disk the pre-lock read picks it up too and the staleness never
        occurs. So the name mutex stands in for the concurrent winner and writes the
        pin as we acquire.
        """
        import contextlib as _contextlib

        c = self._prep(tmp_path, monkeypatch)
        derived = rt.derive_port(c, "demo")
        fallback = derived + 7
        c.pods_dir.mkdir(parents=True, exist_ok=True)

        @_contextlib.contextmanager
        def _mutex_that_loses_the_race(_cfg, _name):
            # The other `up` got here first: it pinned its fallback and started the
            # pod while we were blocked on this lock. Written DIRECTLY and once --
            # `write_env_file` re-acquires this very mutex, so going through it here
            # would recurse, and `pin_checkout` below re-enters on the same thread.
            if not raced:
                raced.append(True)
                c.env_file("demo").write_text(
                    f"PORT='{fallback}'\n{rt.AUTO_PORT_KEY}='{fallback}'\n"
                )
            yield

        raced: list[bool] = []
        monkeypatch.setattr(rt, "pod_name_mutex", _mutex_that_loses_the_race)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: True)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        probed: list[int] = []
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p: (probed.append(p), 403)[1])

        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )

        assert probed == [fallback], (
            "the health wait must use the port resolved INSIDE the lock "
            f"({fallback}), not the pre-lock derivation ({derived})"
        )

    def test_allocation_and_start_happen_under_the_plane_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two DIFFERENT colliding names hold disjoint NAME locks, so the claim has
        to be serialized plane-wide or both can probe one port free and both boot."""
        import contextlib as _contextlib

        c = self._prep(tmp_path, monkeypatch)
        order: list[str] = []

        @_contextlib.contextmanager
        def _tracking_plane(_cfg):
            order.append("plane-acquire")
            try:
                yield
            finally:
                order.append("plane-release")

        monkeypatch.setattr(rt, "pod_plane_mutex", _tracking_plane)
        monkeypatch.setattr(
            rt, "allocate_port", lambda cfg, n: (order.append("allocate"), (7811, None))[1]
        )
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "start_pod", lambda cfg, n: (order.append("start"), _cp())[1])
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)

        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )

        assert order == ["plane-acquire", "allocate", "start", "plane-release"], (
            "the claim must be held from choosing the port through starting the pod "
            f"-- got {order}"
        )

    def test_up_missing_worktree_teaches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        c = self._prep(tmp_path, monkeypatch, ready=False)
        with pytest.raises(SystemExit):
            pod_cli._up(
                c, argparse.Namespace(name="ghost", json=False, seed="", ttl="2h", provision=False)
            )
        assert "git worktree add" in capsys.readouterr().err  # git-native resolution

    def test_up_no_dist_points_at_provision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        c = self._prep(tmp_path, monkeypatch, dist=False)  # venv but no dist
        with pytest.raises(SystemExit):
            pod_cli._up(
                c, argparse.Namespace(name="demo", json=False, seed="", ttl="2h", provision=False)
            )
        assert "--provision" in capsys.readouterr().err

    def test_up_broken_gateway_fails_fast(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        c = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p: -1)
        monkeypatch.setattr(rt, "recent_journal", lambda cfg, n, ln=30: "ImportError: boom")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        with pytest.raises(SystemExit):
            pod_cli._up(
                c, argparse.Namespace(name="demo", json=False, seed="", ttl="2h", provision=False)
            )
        err = capsys.readouterr().err
        assert "worktree build, not pod" in err and "ImportError: boom" in err


class TestReviewRound1Fixes:
    """Round-1 review fixes: deny-by-default channel disable in the sanitized seed,
    WeCom env-cred scrub, ttl query quoting, matched-quote env parsing."""

    def test_seed_forces_all_channels_off(self, tmp_path: Path) -> None:
        seed = tmp_path / "s"
        seed.mkdir()
        (seed / "config.json").write_text(
            json.dumps(
                {
                    "tunnel": {"enabled": True},
                    "telegram": {"enabled": True, "bot_token": "tg-live"},
                    "wecom": {"enabled": True},
                }
            )
        )
        out = rt.sanitized_seed_config(seed)
        assert out is not None
        assert out["tunnel"]["enabled"] is False
        assert out["telegram"]["enabled"] is False
        assert out["wecom"]["enabled"] is False
        assert out["telegram"]["bot_token"] == "tg-live"  # preserved, just disabled

    def test_build_pod_env_scrubs_wecom_and_telegram(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WECOM_BOT_ID", "bot-live")
        monkeypatch.setenv("WECOM_SECRET", "sec-live")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-live")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "sts-temp")
        env = rt.build_pod_env(cfg, tmp_path / "home", 7999, tmp_path / "co")
        assert "WECOM_BOT_ID" not in env
        assert "WECOM_SECRET" not in env
        assert "TELEGRAM_BOT_TOKEN" not in env  # non-AWS *_TOKEN
        assert env.get("AWS_SESSION_TOKEN") == "sts-temp"  # AWS kept

    def test_seed_forces_feishu_off(self, tmp_path: Path) -> None:
        """A seed cloned from a real home must not boot a live Feishu bot.

        ``feishu.enabled`` self-activates through ``maybe_start_feishu`` exactly
        like telegram/wecom, so it belongs in the sanitize roster. Other keys
        survive so the pod's config still round-trips.
        """
        seed = tmp_path / "s"
        seed.mkdir()
        (seed / "config.json").write_text(
            json.dumps({"feishu": {"enabled": True, "allowed_open_ids": ["ou_live"]}})
        )
        out = rt.sanitized_seed_config(seed)
        assert out is not None
        assert out["feishu"]["enabled"] is False
        assert out["feishu"]["allowed_open_ids"] == ["ou_live"]

    def test_build_pod_env_scrubs_feishu(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FEISHU_APP_ID / FEISHU_APP_SECRET match none of the other predicates.

        Neither ends in ``_TOKEN`` and neither carries the SLACK_/WECOM_ prefix,
        so without an explicit ``FEISHU_`` prefix a pod inherits the live plane's
        Feishu app identity from the systemd --user manager env.
        """
        monkeypatch.setenv("FEISHU_APP_ID", "cli-live")
        monkeypatch.setenv("FEISHU_APP_SECRET", "sec-live")
        env = rt.build_pod_env(cfg, tmp_path / "home", 7999, tmp_path / "co")
        assert "FEISHU_APP_ID" not in env
        assert "FEISHU_APP_SECRET" not in env

    def test_read_env_file_matched_quote_pair_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
        c = PodConfig.load()
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        # An inner apostrophe survives; only the one surrounding pair is stripped.
        c.env_file("x").write_text("CHECKOUT='/a/o'brien'\n")
        assert rt.read_env_file(c, "x")["CHECKOUT"] == "/a/o'brien"

    def test_mint_token_quotes_ttl(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / ".local_secret").write_text("s")
        captured: dict[str, str] = {}

        class _Resp:
            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"token":"t"}'

        def _urlopen(req: object, timeout: int = 5) -> "_Resp":
            captured["url"] = req.full_url  # type: ignore[attr-defined]
            return _Resp()

        # Mint now requires positive ownership proof; this test is about the URL
        # it builds, so grant the proof rather than exercising the guard here.
        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_POD)
        monkeypatch.setattr(rt, "loopback_urlopen", _urlopen)
        rt.mint_token(c, "demo", "1 h")
        assert "ttl=1%20h" in captured["url"]


class TestReviewRound2Fix:
    """Round-2 review fix: per-pod env values must be single-line (fail-closed)."""

    def test_write_env_file_rejects_newline_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
        c = PodConfig.load()
        with pytest.raises(rt.PodError):
            rt.write_env_file(c, "x", {"SEED": "/a/\nevil"})
        # a normal single-line value still round-trips
        rt.write_env_file(c, "y", {"CHECKOUT": "/a/b"})
        assert rt.read_env_file(c, "y")["CHECKOUT"] == "/a/b"


@pytest.mark.skipif(
    rt.fcntl is None,
    reason="flock needs POSIX; without it the mutex is a documented no-op",
)
class TestEnvFileConcurrentWrite:
    """``write_env_file`` merges, so it must serialize per pod name.

    ``pod up`` writes pod settings AND starts the unit whose gateway writes the same
    file, so an unserialized merge drops one side's keys and boots the pod on stale
    config.

    Both tests assert a property only the real lock provides, so both are POSIX-only:
    where ``fcntl`` is absent ``pod_name_mutex`` degrades to a no-op by design, and
    pods are refused on those hosts anyway.
    """

    def test_a_concurrent_write_does_not_drop_the_other_writers_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two real threads, both past a barrier before either takes the lock.

        Threads rather than a nested call because the lock is per open-file-description:
        re-entering from one thread exercises the reentrant counter instead of the
        cross-writer exclusion this is about.
        """
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
        c = PodConfig.load()
        rt.write_env_file(c, "demo", {"CHECKOUT": "/first", "APPROVAL": "reads"})

        entered = threading.Barrier(2, timeout=30)
        errors: list[BaseException] = []

        def writer(updates: dict[str, str]) -> None:
            try:
                entered.wait()
                rt.write_env_file(c, "demo", updates)
            except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=({"CRONS": "1"},)),
            threading.Thread(target=writer, args=({"SEED": "/s"},)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), "write_env_file deadlocked"
        assert not errors, f"writer raised: {errors!r}"

        final = rt.read_env_file(c, "demo")
        # Neither writer's key may be lost, and the pre-existing ones survive both.
        assert final["CRONS"] == "1"
        assert final["SEED"] == "/s"
        assert final["CHECKOUT"] == "/first"
        assert final["APPROVAL"] == "reads"

    def test_a_writer_inside_the_pod_up_transaction_is_serialized_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mutex must be the one ``pod up`` holds, not one private to the merge.

        ``pod up`` runs its whole transaction under :func:`pod_name_mutex`, so a lock
        only ``write_env_file`` took would leave the writes made inside it unexcluded.
        Holding the mutex here must therefore block a competing writer outright.
        """
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
        c = PodConfig.load()
        rt.write_env_file(c, "demo", {"CHECKOUT": "/first"})

        blocked = threading.Event()

        def competing_writer() -> None:
            rt.write_env_file(c, "demo", {"SEED": "/s"})
            blocked.set()

        with rt.pod_name_mutex(c, "demo"):
            t = threading.Thread(target=competing_writer)
            t.start()
            # The transaction holds the mutex, so the other writer cannot proceed.
            assert not blocked.wait(timeout=1.0), "a writer entered during the transaction"
            rt.write_env_file(c, "demo", {"APPROVAL": "yolo"})
        t.join(timeout=30)
        assert not t.is_alive()

        final = rt.read_env_file(c, "demo")
        assert final["APPROVAL"] == "yolo"
        assert final["SEED"] == "/s"
        assert final["CHECKOUT"] == "/first"

    def test_a_lock_free_reader_never_sees_a_torn_env_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The write must be atomic, not merely serialized.

        ``boot`` reads without the mutex on purpose, so an in-place truncating
        rewrite lets a reader observe one generation spliced onto another. A
        missing ``APPROVAL`` is the least restrictive outcome (``boot`` leaves
        ``approval_mode`` unset, which falls through to auto-approve), so a torn
        read is a silent privilege upgrade rather than a crash.

        Modelled deterministically instead of by racing: a reader opens the file
        and consumes half of it, a write lands, then it consumes the rest. Under
        temp-file + rename its descriptor still refers to the intact old inode,
        so the two halves belong to ONE generation. Under an in-place rewrite the
        same descriptor reads across the truncation and the halves do not match
        any generation that was ever valid.
        """
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
        c = PodConfig.load()
        # Enough keys that a spliced read is unambiguous rather than a near-miss.
        first = {"APPROVAL": "interactive", "CHECKOUT": "/a"}
        first.update({f"PAD{i}": f"v{i}" * 8 for i in range(40)})
        rt.write_env_file(c, "demo", first)

        env_path = c.env_file("demo")
        before = env_path.read_bytes()

        # Raw fd, NOT open() -- a buffered text reader slurps a small file whole
        # on the first read, so the second read would come from memory and the
        # test could not observe the on-disk seam at all.
        fd = os.open(str(env_path), os.O_RDONLY)
        try:
            head = os.read(fd, len(before) // 2)
            # The writer runs while this reader is mid-file.
            rt.write_env_file(c, "demo", {"APPROVAL": "yolo"})
            chunks = [head]
            while True:
                part = os.read(fd, 4096)
                if not part:
                    break
                chunks.append(part)
        finally:
            os.close(fd)
        seen = b"".join(chunks)

        after = env_path.read_bytes()
        assert head, "reader consumed nothing; the fixture is not exercising the seam"
        # The reader must have seen exactly one whole generation, old or new.
        assert seen in (before, after), "torn read: the reader spliced two generations together"
        # And the new generation must be complete on disk.
        final = rt.read_env_file(c, "demo")
        assert final["APPROVAL"] == "yolo"
        assert final["CHECKOUT"] == "/a"


class TestUnitExecSelfHeal:
    """The unit bakes an absolute kirocrew path; a pruned worktree leaves it
    dangling and every start fails EXEC. unit_exec_ok detects that."""

    def _cfg_with_unit(self, tmp_path, monkeypatch, exec_line):
        from kiro_crew.pod import unit as unit_mod
        from kiro_crew.pod.config import PodConfig

        monkeypatch.setattr(unit_mod, "unit_path", lambda cfg: tmp_path / "pod@.service")
        (tmp_path / "pod@.service").write_text(f"[Service]\n{exec_line}\nRestart=on-failure\n")
        return PodConfig.load()

    def test_dangling_binary_detected(self, tmp_path, monkeypatch):
        from kiro_crew.pod import unit as unit_mod

        cfg = self._cfg_with_unit(
            tmp_path,
            monkeypatch,
            f"ExecStart={(tmp_path / 'gone/.venv/bin/kirocrew').as_posix()} pod _run %i",
        )
        assert unit_mod.unit_exec_ok(cfg) is False

    def test_valid_binary_passes(self, tmp_path, monkeypatch):
        from kiro_crew.pod import unit as unit_mod

        exe = tmp_path / "kirocrew"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        cfg = self._cfg_with_unit(tmp_path, monkeypatch, f"ExecStart={exe.as_posix()} pod _run %i")
        assert unit_mod.unit_exec_ok(cfg) is True

    def test_missing_unit_file_detected(self, tmp_path, monkeypatch):
        from kiro_crew.pod import unit as unit_mod
        from kiro_crew.pod.config import PodConfig

        monkeypatch.setattr(unit_mod, "unit_path", lambda cfg: tmp_path / "absent@.service")
        assert unit_mod.unit_exec_ok(PodConfig.load()) is False

    def test_a_unit_carrying_the_removed_teardown_hook_is_not_current(self, tmp_path, monkeypatch):
        """Units are written once by `pod install`, so on UPGRADE a machine keeps
        whatever it installed. Without this, an older unit's ExecStopPost would go
        on racing the pod's own subprocesses (and wiping the HOME on the stop half
        of a Restart=) until someone reinstalled by hand."""
        from kiro_crew.pod import unit as unit_mod

        exe = tmp_path / "kirocrew"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        cfg = self._cfg_with_unit(tmp_path, monkeypatch, f"ExecStart={exe.as_posix()} pod _run %i")
        unit_path = tmp_path / "pod@.service"
        assert unit_mod.unit_is_current(cfg) is True  # what this build renders
        unit_path.write_text(unit_path.read_text() + f"ExecStopPost={exe} pod _cleanup %i\n")
        assert unit_mod.unit_is_current(cfg) is False

    @requires_posix_pod_lifecycle
    def test_what_this_build_renders_is_current(self, cfg, monkeypatch, tmp_path):
        """Guard against the reverse failure: a check that flagged the CURRENT
        template would re-render and daemon-reload on every single `pod up`."""
        from kiro_crew.pod import unit as unit_mod

        monkeypatch.setattr(unit_mod, "unit_path", lambda c: tmp_path / "pod@.service")
        # An executable that exists on every platform the suite runs on — a POSIX
        # path here made unit_exec_ok report "stale" on Windows.
        monkeypatch.setattr(unit_mod, "_kirocrew_argv", lambda: (sys.executable,))
        unit_mod.install_unit(cfg)
        assert unit_mod.unit_is_current(cfg) is True

    @requires_posix_pod_lifecycle
    def test_spaced_executable_is_quoted_and_current(self, cfg, monkeypatch, tmp_path):
        from kiro_crew.pod import unit as unit_mod

        exe = tmp_path / "venv with spaces" / "kirocrew"
        exe.parent.mkdir()
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        monkeypatch.setattr(unit_mod, "unit_path", lambda c: tmp_path / "pod@.service")
        monkeypatch.setattr(unit_mod, "_kirocrew_argv", lambda: (str(exe),))
        rendered = unit_mod.install_unit(cfg).read_text()
        assert 'ExecStart="' in rendered
        assert unit_mod.unit_is_current(cfg) is True

    def test_malformed_execstart_is_stale(self, tmp_path, monkeypatch):
        from kiro_crew.pod import unit as unit_mod

        cfg = self._cfg_with_unit(tmp_path, monkeypatch, 'ExecStart="unterminated')
        assert unit_mod.unit_exec_ok(cfg) is False

    def test_start_pod_reinstalls_a_stale_unit_before_booting_it(self, cfg, monkeypatch):
        steps: list[str] = []
        monkeypatch.setattr(rt.unit_mod, "unit_is_current", lambda c: False)
        monkeypatch.setattr(rt.unit_mod, "install_unit", lambda c: steps.append("reinstall"))
        monkeypatch.setattr(
            rt.unit_mod, "install_dropin", lambda c, n, co: steps.append("pin-checkout")
        )
        monkeypatch.setattr(rt, "read_env_file", lambda c, n: {"CHECKOUT": "/checkouts/demo"})

        def _systemctl(*args, **kwargs):
            steps.append(args[0])
            return _cp()

        monkeypatch.setattr(rt, "systemctl", _systemctl)
        assert rt.start_pod(cfg, "demo").returncode == 0
        assert steps == [
            "reinstall",
            "daemon-reload",
            "pin-checkout",
            "daemon-reload",
            "start",
        ]

    def test_module_invocation_form_passes(self, tmp_path, monkeypatch):
        from kiro_crew.pod import unit as unit_mod

        cfg = self._cfg_with_unit(
            tmp_path,
            monkeypatch,
            "ExecStart=python3 -m kiro_crew pod _run %i",
        )
        assert unit_mod.unit_exec_ok(cfg) is True


class TestPlatformGuard:
    """Pods are Linux `systemd --user` only (pod/README.md → Platform).

    Off-Linux the verbs must report the refusal, NOT dump a bare
    ``FileNotFoundError`` traceback from the first ``subprocess.run(["systemctl",
    ...])``. The gate lives in ``require_systemd()``, which every systemd/
    journalctl call funnels through.
    """

    def test_require_systemd_refuses_off_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rt, "IS_LINUX", False)
        monkeypatch.setattr(rt.sys, "platform", "darwin")
        with pytest.raises(rt.PodError) as exc:
            rt.require_systemd()
        # Names the platform AND the supported alternative, so the message is actionable.
        assert "darwin" in str(exc.value)
        assert "dev-backend.sh" in str(exc.value)

    def test_require_systemd_refuses_when_systemctl_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rt, "IS_LINUX", True)
        monkeypatch.setattr(rt.shutil, "which", lambda _n: None)
        with pytest.raises(rt.PodError, match="systemctl"):
            rt.require_systemd()

    def test_require_systemd_passes_on_linux_with_systemctl(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(rt, "IS_LINUX", True)
        monkeypatch.setattr(rt.shutil, "which", lambda _n: "/usr/bin/systemctl")
        # Third gate: a reachable session bus (see TestSessionBus).
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        (tmp_path / "bus").touch()
        rt.require_systemd()  # must not raise

    def test_systemctl_gated_before_spawn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The guard runs BEFORE the subprocess, so no spawn is attempted."""
        monkeypatch.setattr(rt, "IS_LINUX", False)
        spawned: list[list[str]] = []
        monkeypatch.setattr(rt, "_run", lambda cmd, **k: spawned.append(cmd))
        with pytest.raises(rt.PodError):
            rt.systemctl("list-units")
        assert spawned == []

    def test_recent_journal_gated(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        """journalctl is a sibling of systemctl, not routed through it — gate it too."""
        monkeypatch.setattr(rt, "IS_LINUX", False)
        with pytest.raises(rt.PodError):
            rt.recent_journal(cfg, "alpha")

    def test_ls_reports_cleanly_off_linux(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """`pod ls` exits 1 with a one-liner — the dispatch layer converts PodError."""
        monkeypatch.setattr(rt, "IS_LINUX", False)
        args = argparse.Namespace(pod_action="ls", json=False)
        with pytest.raises(SystemExit) as exc:
            pod_cli.dispatch(args)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "pod:" in err and "Linux" in err
        assert "Traceback" not in err

    def test_install_writes_no_unit_off_linux(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate precedes install_unit(), so no unit file is left behind."""
        monkeypatch.setattr(rt, "IS_LINUX", False)
        wrote: list[object] = []
        monkeypatch.setattr(unit_mod, "install_unit", lambda c: wrote.append(c))
        with pytest.raises(rt.PodError):
            pod_cli._install(cfg, argparse.Namespace())
        assert wrote == []


class TestSessionBus:
    """``systemctl --user`` needs the per-user systemd instance's bus pointers.

    A process descended from a systemd SYSTEM unit — how ``kirocrew service
    install`` runs the gateway — inherits no login-session environment, so
    neither ``XDG_RUNTIME_DIR`` nor ``DBUS_SESSION_BUS_ADDRESS`` is set and every
    pod verb died with "Failed to connect to bus: No medium found" even though
    the bus socket existed. ``_systemctl_env()`` backfills them; when the socket
    is genuinely absent ``require_systemd()`` explains the fix instead.
    """

    @staticmethod
    def _bus(monkeypatch: pytest.MonkeyPatch, runtime_dir: Path, *, exists: bool) -> Path:
        """Point at *runtime_dir* as the session runtime dir, with no inherited bus."""
        runtime_dir.mkdir(parents=True, exist_ok=True)
        sock = runtime_dir / "bus"
        if exists:
            sock.touch()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        return sock

    def test_backfills_both_when_socket_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sock = self._bus(monkeypatch, tmp_path / "run", exists=True)
        env = rt._systemctl_env()
        assert env["XDG_RUNTIME_DIR"] == str(tmp_path / "run")
        assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={sock}"

    def test_derives_runtime_dir_from_uid_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No ``XDG_RUNTIME_DIR`` → systemd's conventional ``/run/user/<uid>``."""
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        monkeypatch.setattr(rt.os, "getuid", lambda: 4242, raising=False)
        assert rt._systemctl_env()["XDG_RUNTIME_DIR"] == "/run/user/4242"

    def test_leaves_bus_unset_when_socket_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No socket → no invented address, so systemctl emits its own diagnostic
        instead of failing against a path we made up."""
        self._bus(monkeypatch, tmp_path / "run", exists=False)
        env = rt._systemctl_env()
        assert "DBUS_SESSION_BUS_ADDRESS" not in env
        assert env["XDG_RUNTIME_DIR"] == str(tmp_path / "run")

    def test_never_clobbers_an_explicit_bus(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A caller pointing at another bus is left alone — this only ever ADDS."""
        self._bus(monkeypatch, tmp_path / "run", exists=True)
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/elsewhere/bus")
        assert rt._systemctl_env()["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/elsewhere/bus"

    def test_never_clobbers_an_explicit_runtime_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._bus(monkeypatch, tmp_path / "run", exists=True)
        assert rt._systemctl_env()["XDG_RUNTIME_DIR"] == str(tmp_path / "run")

    def test_has_session_bus_trusts_an_explicit_address(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An explicit address may not be a filesystem path at all — take it as given."""
        self._bus(monkeypatch, tmp_path / "run", exists=False)
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:abstract=/tmp/dbus-abc")
        assert rt.has_session_bus() is True

    def test_require_systemd_explains_a_missing_bus(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The raw systemctl message names neither cause nor fix — ours does."""
        monkeypatch.setattr(rt, "IS_LINUX", True)
        monkeypatch.setattr(rt.shutil, "which", lambda _n: "/usr/bin/systemctl")
        sock = self._bus(monkeypatch, tmp_path / "run", exists=False)
        monkeypatch.setenv("USER", "tester")
        with pytest.raises(rt.PodError) as exc:
            rt.require_systemd()
        msg = str(exc.value)
        assert str(sock) in msg
        assert "loginctl enable-linger tester" in msg
        # Keyed on the socket's absence, not on matching systemctl's stderr.
        assert "No medium found" not in msg

    def test_missing_bus_is_reported_before_any_spawn(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(rt, "IS_LINUX", True)
        monkeypatch.setattr(rt.shutil, "which", lambda _n: "/usr/bin/systemctl")
        self._bus(monkeypatch, tmp_path / "run", exists=False)
        spawned: list[list[str]] = []
        monkeypatch.setattr(rt, "_run", lambda cmd, **k: spawned.append(cmd))
        with pytest.raises(rt.PodError):
            rt.systemctl("list-units")
        assert spawned == []

    def test_systemctl_env_is_the_only_env_source_for_systemd_calls(self) -> None:
        """Anti-regression: a future direct ``subprocess.run(["systemctl", ...])``
        that forgets ``env=_systemctl_env()`` would silently reintroduce the bug,
        so pin every systemd/journalctl spawn in the module to that one source.
        """
        import ast

        src = Path(rt.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)

        def _is_subprocess_run(node: ast.Call) -> bool:
            fn = node.func
            return (
                isinstance(fn, ast.Attribute)
                and fn.attr == "run"
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "subprocess"
            )

        def _uses_systemctl_env(node: ast.Call) -> bool:
            for kw in node.keywords:
                if kw.arg != "env":
                    continue
                val = kw.value
                return (
                    isinstance(val, ast.Call)
                    and isinstance(val.func, ast.Name)
                    and val.func.id == "_systemctl_env"
                )
            return False

        literal_systemd = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_subprocess_run(node):
                continue
            argv = node.args[0] if node.args else None
            if not isinstance(argv, ast.List):
                continue
            head = argv.elts[0] if argv.elts else None
            if not isinstance(head, ast.Constant) or head.value not in (
                "systemctl",
                "journalctl",
            ):
                continue
            literal_systemd += 1
            assert _uses_systemctl_env(
                node
            ), f"line {node.lineno}: {head.value} spawned without env=_systemctl_env()"

        # journalctl in recent_journal is the one literal systemd spawn; if that
        # drops to zero the scan above has stopped covering anything.
        assert literal_systemd >= 1, "no literal systemd spawn found — scan is inert"
        # The variable-argv chokepoint every systemctl() call funnels through.
        chokepoint = next(
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_run"
        )
        assert any(
            _is_subprocess_run(n) and _uses_systemctl_env(n)
            for n in ast.walk(chokepoint)
            if isinstance(n, ast.Call)
        ), "_run no longer passes env=_systemctl_env()"


@requires_posix_pod_lifecycle
class TestBootTimeSettings:
    """``pod up --approval`` / ``--crons`` are persisted per pod and applied at boot.

    Neither can ride the unit file: both backends re-enter the pod as
    ``kirocrew pod _run <name>`` with no flags. On systemd one template unit is
    shared by every instance, so it cannot carry per-pod flags; launchd writes a
    per-pod plist but still execs that same flagless argv. So they travel through
    the per-pod env file, exactly as ``SEED`` does.
    """

    def _booted_argv(
        self, root: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
    ) -> list[str]:
        """Boot a ready pod with *env* merged into its env file; return the exec argv."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(root / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(root / "pods"))
        c = PodConfig.load()
        rt.pin_checkout(c, "x", _ready_worktree(root, "x"))
        if env:
            rt.write_env_file(c, "x", env)
        seen: list[list[str]] = []
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(os, "execve", lambda path, argv, e: seen.append(argv))
        rt.boot(c, "x")
        assert len(seen) == 1, "boot did not exec exactly once"
        return seen[0][1:]  # drop argv[0], the venv binary path

    def test_boot_forwards_the_recorded_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argv = self._booted_argv(tmp_path, monkeypatch, {"APPROVAL": "reads"})
        assert argv == ["gateway", "--no-crons", "--no-tunnel", "--approval", "reads"]

    def test_boot_argv_unchanged_when_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The no-regression pin: a pod created before the APPROVAL key existed,
        # or created without it, boots with no --approval at all rather than
        # picking one.
        argv = self._booted_argv(tmp_path, monkeypatch, {})
        assert argv == ["gateway", "--no-crons", "--no-tunnel"]

    def test_boot_always_refuses_to_publish_a_tunnel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # --no-tunnel is UNCONDITIONAL for any checkout that accepts it: unlike
        # CRONS there is no env key to opt a pod back into publishing, because a
        # throwaway instance has no business being reachable off the host. Pinned
        # across every combination the env file can express, so an opt-in cannot
        # be added by accident later.
        for i, env in enumerate(
            (
                {},
                {"CRONS": "1"},
                {"APPROVAL": "yolo"},
                {"CRONS": "1", "APPROVAL": "interactive"},
            )
        ):
            argv = self._booted_argv(tmp_path / f"nt{i}", monkeypatch, env)
            assert "--no-tunnel" in argv, env

    def test_boot_omits_the_flag_for_a_checkout_that_cannot_parse_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        # The control plane builds this argv but the TARGET WORKTREE's gateway
        # executes it, and Dev Fleet worktrees sit at different commits. Handing
        # --no-tunnel to a gateway that predates it makes argparse exit 2, and the
        # unit's Restart=on-failure/RestartSec=5 (no RestartPreventExitStatus)
        # turns that into a restart loop every 5s.
        #
        # So the flag is dropped and the pod STILL BOOTS. Such a checkout simply
        # does not receive the new guarantee -- exactly its behaviour before the
        # flag existed.
        root = tmp_path / "old"
        root.mkdir()
        c = PodConfig.load()
        rt.pin_checkout(c, "x", _ready_worktree(root, "x", flags=False))
        # Seed the HOME with tunnel.enabled=TRUE. boot() must leave it ALONE: a
        # boot-time config re-pin was tried and withdrawn, because
        # KiroCrewConfig.load() deep-merges config.local.json OVER config.json with
        # the overlay winning (and `config set` writes the overlay by default), so
        # pinning this file does not pin the setting -- while writing it does mutate
        # an operator's file. Starting from true is what makes the assertion below
        # able to observe a re-pin if one is ever reintroduced.
        home = c.home_dir("x")
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        (home / "config.json").write_text(json.dumps({"tunnel": {"enabled": True}}))
        seen: list[list[str]] = []
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(os, "execve", lambda path, argv, e: seen.append(argv))
        rt.boot(c, "x")

        assert seen, "boot did not exec"
        argv = seen[0][1:]
        assert "--no-tunnel" not in argv
        # It still boots -- an old pod keeps working rather than restart-looping.
        assert argv[0] == "gateway"
        assert "keeps the tunnel behaviour it had" in capsys.readouterr().out
        # And the pod's config is untouched, not rewritten behind the operator.
        assert json.loads((home / "config.json").read_text())["tunnel"]["enabled"] is True

    def test_the_probe_accepts_this_very_repo(self) -> None:
        """The ratchet: THIS checkout must satisfy its own probe.

        Without this, a refactor that moves the gateway flag declarations out of
        ``cli.py`` (or changes how they are quoted) would silently DROP the flag for
        EVERY current-checkout pod with nothing going red -- each one falling back to
        its pre-flag tunnel behaviour while still looking guarded. Failing here is
        the signal to teach ``target_supports_flag`` the new location, not to relax
        it.
        """
        repo_root = Path(rt.__file__).resolve().parents[3]
        assert (repo_root / "src" / "kiro_crew" / "cli.py").is_file(), repo_root
        assert rt.target_supports_flag(repo_root, "--no-tunnel") is True
        assert rt.target_supports_flag(repo_root, "--no-crons") is True
        # A flag this repo does not declare must still read as unsupported, so the
        # probe is discriminating rather than trivially true.
        assert rt.target_supports_flag(repo_root, "--no-such-flag-exists") is False

    def test_the_probe_ignores_a_comment_only_mention(self, tmp_path: Path) -> None:
        # A checkout that merely TALKS about the flag does not parse it. Treating a
        # comment as a declaration would pass the flag and produce exactly the
        # argparse-exit restart loop the probe exists to prevent.
        co = tmp_path / "co"
        (co / "src" / "kiro_crew").mkdir(parents=True)
        cli = co / "src" / "kiro_crew" / "cli.py"

        cli.write_text('# TODO: someday accept "--no-tunnel" here\ngw.add_argument("--x")\n')
        assert rt.target_supports_flag(co, "--no-tunnel") is False

        # Same file, real declaration present -> supported.
        cli.write_text(
            '# TODO: someday accept "--no-tunnel" here\n'
            "gw_parser.add_argument(\n"
            '    "--no-tunnel",\n'
            '    action="store_true",\n'
            ")\n"
        )
        assert rt.target_supports_flag(co, "--no-tunnel") is True

    def test_the_flag_probe_reads_the_target_checkouts_own_source(self, tmp_path: Path) -> None:
        # The probe reads the checkout that will EXECUTE, not this process's own
        # source -- that distinction is the entire point, so it is asserted
        # directly rather than only through boot().
        co = tmp_path / "co"
        (co / "src" / "kiro_crew").mkdir(parents=True)
        cli = co / "src" / "kiro_crew" / "cli.py"

        cli.write_text('gw.add_argument("--no-crons")\n')
        assert rt.target_supports_flag(co, "--no-tunnel") is False

        cli.write_text('gw.add_argument("--no-tunnel")\n')
        assert rt.target_supports_flag(co, "--no-tunnel") is True

        # Unreadable source answers False, so an unparseable checkout degrades to
        # "omit the flag" rather than raising on the boot path.
        assert rt.target_supports_flag(tmp_path / "nope", "--no-tunnel") is False

    def test_boot_forces_interactive_on_an_unknown_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        # The env file is hand-editable, so boot re-validates. It must NOT merely
        # drop the value: omitting --approval leaves approval_mode unset, and the
        # gateway then falls through to cfg.agent.approval_mode, which
        # config/loader.py defaults to "auto" -- auto-approve every tool.
        # Dropping would be the LEAST restrictive outcome, so boot pins
        # interactive explicitly.
        argv = self._booted_argv(tmp_path, monkeypatch, {"APPROVAL": "--not-a-mode"})
        assert argv == ["gateway", "--no-crons", "--no-tunnel", "--approval", "interactive"]
        assert "ignoring unknown APPROVAL" in capsys.readouterr().out

    def test_every_declared_mode_survives_boot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Behavioural pin only: each mode in the enforcement tuple round-trips
        # through the env file into argv. This does NOT guard parser drift --
        # a mode present in cli.py but absent from APPROVAL_MODES is never
        # iterated here. test_cli_approval_choices_match_the_enforcement_tuple
        # covers that.
        for mode in rt.APPROVAL_MODES:
            argv = self._booted_argv(tmp_path / mode, monkeypatch, {"APPROVAL": mode})
            assert argv[-2:] == ["--approval", mode]

    @staticmethod
    def _top_level_cli_ast() -> ast.Module:
        # rt.__file__ is pod/runtime.py, so parent.parent is the package root.
        # encoding is explicit: cli.py contains non-ASCII (emoji in guard
        # messages), and read_text() defaults to the platform locale codec,
        # which is cp1252 on Windows CI and raises UnicodeDecodeError there.
        src = (Path(rt.__file__).parent.parent / "cli.py").read_text(encoding="utf-8")
        return ast.parse(src)

    def test_cli_approval_choices_match_the_enforcement_tuple(self) -> None:
        # cli.py repeats the choices literal instead of importing pod.runtime
        # (the top-level parser must not import pod modules at startup), so the
        # duplication is real. Read the literal argparse actually registers, so a
        # mode added on one side only fails HERE rather than being accepted by
        # argparse and then dropped at boot. Iterating APPROVAL_MODES alone
        # cannot catch that, because the missing mode is not in it.
        choices: list[list[str]] = []
        for node in ast.walk(self._top_level_cli_ast()):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "add_argument"):
                continue
            if not (isinstance(fn.value, ast.Name) and fn.value.id == "pod_up"):
                continue
            if not any(isinstance(a, ast.Constant) and a.value == "--approval" for a in node.args):
                continue
            for kw in node.keywords:
                if kw.arg == "choices" and isinstance(kw.value, ast.List):
                    choices.append([e.value for e in kw.value.elts if isinstance(e, ast.Constant)])
        assert choices == [list(rt.APPROVAL_MODES)], (
            "pod up --approval choices must match runtime.APPROVAL_MODES exactly; parser "
            f"declares {choices}, enforcement tuple is {list(rt.APPROVAL_MODES)}"
        )

    def test_every_pod_subparser_name_is_assigned(self) -> None:
        # A `--crons` edit deleted `pod_down = pod_sub.add_parser(...)`, leaving
        # pod_down undefined: a NameError at parser build. That is invisible to
        # py_compile AND to every test in this file, which imports pod.cli and
        # never the top-level parser. Pin the structure so it cannot recur.
        tree = self._top_level_cli_ast()
        assigned = {
            t.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for t in node.targets
            if isinstance(t, ast.Name) and t.id.startswith("pod_")
        }
        used = {
            n.value.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id.startswith("pod_")
        }
        assert used <= assigned, f"pod_* names used but never assigned: {sorted(used - assigned)}"

    def _prep_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, active: bool
    ) -> PodConfig:
        monkeypatch.setenv("KIROCREW_POD_WORKTREES_ROOT", str(tmp_path / "wts"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setattr(rt, "_git_worktrees", lambda ref: {})
        _ready_worktree(tmp_path / "wts", "demo")
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: active)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        return PodConfig.load()

    def test_up_records_the_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        c = self._prep_up(tmp_path, monkeypatch, active=False)
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo", json=False, seed="", ttl="2h", provision=False, approval="yolo"
            ),
        )
        assert rt.read_env_file(c, "demo").get("APPROVAL") == "yolo"

    def test_up_on_a_running_pod_notes_the_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        c = self._prep_up(tmp_path, monkeypatch, active=True)
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo", json=False, seed="", ttl="2h", provision=False, approval="reads"
            ),
        )
        err = capsys.readouterr().err
        assert "already running" in err and "pod down demo" in err
        # Recorded either way, so the next boot picks it up.
        assert rt.read_env_file(c, "demo").get("APPROVAL") == "reads"

    def test_up_tolerates_a_namespace_without_the_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``provision`` is read the same defensive way, and hand-built Namespaces
        # (here and in TestUpVerb) must not have to carry every optional key.
        c = self._prep_up(tmp_path, monkeypatch, active=False)
        pod_cli._up(
            c, argparse.Namespace(name="demo", json=False, seed="", ttl="2h", provision=False)
        )
        assert "APPROVAL" not in rt.read_env_file(c, "demo")

    def test_up_audits_the_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # `yolo` auto-approves every tool, so the SEL trail must name the mode
        # rather than recording only that a pod came up.
        c = self._prep_up(tmp_path, monkeypatch, active=False)
        seen: list[tuple] = []
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: seen.append(a))
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo", json=False, seed="", ttl="2h", provision=False, approval="yolo"
            ),
        )
        allowed = [a for a in seen if a[:2] == ("pod.up", "allowed")]
        assert allowed, "pod.up allowed was never audited"
        assert "approval=yolo" in allowed[0][2]

    # --- crons -------------------------------------------------------------

    def test_boot_enables_the_scheduler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # --no-crons is dropped, which is how the gateway turns the scheduler on.
        argv = self._booted_argv(tmp_path, monkeypatch, {"CRONS": "1"})
        assert argv == ["gateway", "--no-tunnel"]

    def test_boot_accepts_alternative_truthy_spellings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The env file is hand-editable, so the obvious spellings are honoured.
        for i, raw in enumerate(("true", "YES", " on ")):
            argv = self._booted_argv(tmp_path / f"t{i}", monkeypatch, {"CRONS": raw})
            assert argv == ["gateway", "--no-tunnel"], raw

    def test_boot_ignores_an_unrecognised_crons_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        # Falls back to the safer setting (scheduler off, the pre-existing
        # behavior) rather than guessing, and the pod still boots.
        argv = self._booted_argv(tmp_path, monkeypatch, {"CRONS": "maybe"})
        assert argv == ["gateway", "--no-crons", "--no-tunnel"]
        assert "ignoring unrecognised CRONS" in capsys.readouterr().out

    def test_boot_combines_crons_and_approval(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argv = self._booted_argv(tmp_path, monkeypatch, {"CRONS": "1", "APPROVAL": "reads"})
        assert argv == ["gateway", "--no-tunnel", "--approval", "reads"]

    def test_up_records_crons(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        c = self._prep_up(tmp_path, monkeypatch, active=False)
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo", json=False, seed="", ttl="2h", provision=False, crons=True
            ),
        )
        assert rt.read_env_file(c, "demo").get("CRONS") == "1"

    def test_up_audits_crons(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A pod with the scheduler on runs work unattended; the trail must say so.
        c = self._prep_up(tmp_path, monkeypatch, active=False)
        seen: list[tuple] = []
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: seen.append(a))
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo", json=False, seed="", ttl="2h", provision=False, crons=True
            ),
        )
        allowed = [a for a in seen if a[:2] == ("pod.up", "allowed")]
        assert allowed, "pod.up allowed was never audited"
        assert "crons=on" in allowed[0][2]

    def test_up_notes_every_deferred_flag_on_a_running_pod(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        # One note covers both settings; a per-flag note would be two messages
        # and the repeated `pod down` instruction would read as two restarts.
        c = self._prep_up(tmp_path, monkeypatch, active=True)
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo",
                json=False,
                seed="",
                ttl="2h",
                provision=False,
                approval="yolo",
                crons=True,
            ),
        )
        err = capsys.readouterr().err
        assert err.count("pod: note:") == 1
        assert "--approval yolo --crons" in err

    def test_up_merges_all_boot_settings_into_one_env_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SEED / APPROVAL / CRONS share one write; the pinned CHECKOUT survives it.
        c = self._prep_up(tmp_path, monkeypatch, active=False)
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo",
                json=False,
                seed="/tmp/fixture",
                ttl="2h",
                provision=False,
                approval="reads",
                crons=True,
            ),
        )
        env = rt.read_env_file(c, "demo")
        assert env.get("SEED") == "/tmp/fixture"
        assert env.get("APPROVAL") == "reads"
        assert env.get("CRONS") == "1"
        # Compare path components, not a "/"-joined suffix: str(Path) uses "\"
        # on Windows, so endswith("wts/demo") fails there.
        checkout = Path(env.get("CHECKOUT", ""))
        assert (checkout.parent.name, checkout.name) == ("wts", "demo")
