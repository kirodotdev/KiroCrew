"""The boot-time child-bootstrap probe, and its refusal wiring.

The pod remaps its kiro-cli child's ``HOME`` so grants die with the pod. When that
broke every ACP spawn, ``/health`` answered 200 the whole time and the only symptom
was ``agent_unreachable`` on each provider -- which reads as a Connections bug, not
a boot failure. These tests pin that a pod whose child cannot bootstrap REFUSES at
``pod up`` with the reason recorded, and that the three viable shapes still boot.

Every case drives the probe with a FAKE child spawned through ``sys.executable``,
so nothing here depends on this host having kiro-cli, on being signed in, or on the
platform being able to exec a ``#!`` script (Windows cannot -- shard 3 failed every
arm with ``WinError 193`` when these fakes were shell scripts).
"""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

import pytest

from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import EXIT_REFUSED_UNRECOVERABLE, PodConfig


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PodConfig:
    """A pod plane under ``tmp_path``.

    ``PodConfig.load()`` roots ``pods_dir`` at the DEFAULT data home on purpose
    (a pod process must find the host's plane, not its own ``KIROCREW_HOME``), so
    the data-home pin does not reach it. Unpinned, the refusal tests below wrote
    ``<name>.refused`` notes into the operator's real ``~/.kiro/crew/pods`` -- and
    failed on a host where that directory did not exist yet, because the note is
    written best-effort and its absence reads as "no refusal".
    """
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "envs"))
    (tmp_path / "envs").mkdir()
    loaded = PodConfig.load()
    assert loaded.pods_dir == tmp_path / "envs", "the pod plane must be this test's own"
    return loaded


def _fake_cli(tmp_path: Path, body: str) -> Path:
    """A Python fake child, runnable on every platform."""
    path = tmp_path / "kiro-cli-fake.py"
    path.write_text(body)
    return path


def _pod_env(tmp_path: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "real-home"),
        "KIROCREW_POD": "1",
        "KIROCREW_OS_HOME": str(tmp_path / "os-home"),
    }


@pytest.fixture
def resolved(monkeypatch: pytest.MonkeyPatch):
    """Point the probe's resolution AND its argv builder at a Python fake.

    Both seams are patched because the probe deliberately runs the real spawn path:
    ``_resolve_kiro_bin`` picks the executable and ``apply_pod_bundle_spawn`` decides
    the final argv. Substituting ``[sys.executable, <script>, <subcmd>]`` keeps that
    path intact while staying executable on Windows.
    """
    from kiro_crew import sandbox
    from kiro_crew.acp import client as acp_client

    def _install(script: Path) -> None:
        # The confinement legs are neutralised for the CLASSIFICATION tests: what
        # they assert is how an outcome is graded, and a real sandbox wrapper would
        # replace argv[0] so the fake child never runs. That the legs ARE applied is
        # pinned separately by TestTheProbeIsConfined below.
        monkeypatch.setattr(sandbox, "wrap_argv", lambda argv, **_kw: (argv, None))
        monkeypatch.setattr(sandbox, "cgroup_scope_argv", lambda argv: argv)
        monkeypatch.setattr(sandbox, "resource_limit_preexec", lambda: None)
        monkeypatch.setattr(acp_client, "_resolve_kiro_bin", lambda **_kw: sys.executable)
        monkeypatch.setattr(
            acp_client,
            "apply_pod_bundle_spawn",
            lambda argv, **_kw: ([sys.executable, str(script), *argv[1:]], False),
        )

    return _install


def test_a_child_that_cannot_bootstrap_refuses_the_boot(tmp_path: Path, resolved) -> None:
    """The regression: an immediate child exit must stop the boot, not reach health 200."""
    resolved(
        _fake_cli(
            tmp_path,
            "import sys\nsys.stderr.write('boom: could not construct sandbox\\n')\n"
            "raise SystemExit(1)\n",
        )
    )

    with pytest.raises(rt.PodError) as excinfo:
        rt._probe_pod_child_bootstrap(_pod_env(tmp_path))

    message = str(excinfo.value)
    assert "exited immediately" in message
    # The recorded reason must carry the child's own words and the remapped HOME,
    # so `pod status` says what failed rather than only that something did.
    assert "could not construct sandbox" in message
    assert str(tmp_path / "os-home") in message


def test_a_child_still_serving_when_the_bound_expires_is_viable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolved
) -> None:
    """Waiting on stdin is what a healthy ACP child does -- accept, do not refuse."""
    monkeypatch.setattr(rt, "_CHILD_VIABILITY_TIMEOUT_SECS", 1.0)
    resolved(_fake_cli(tmp_path, "import time\ntime.sleep(30)\n"))

    rt._probe_pod_child_bootstrap(_pod_env(tmp_path))  # no raise


def test_a_clean_zero_exit_is_viable_not_a_failure(tmp_path: Path, resolved) -> None:
    """The signed-in shape: the probe closes stdin, so a healthy child exits rc=0.

    Red-first for a false refusal that BLOCKED a good pod's boot once the runtime
    auth store was staged: every healthy child then exited 0 with empty stderr, and
    the probe read that as "could not bootstrap" and refused with exit 78.
    """
    resolved(_fake_cli(tmp_path, "raise SystemExit(0)\n"))

    rt._probe_pod_child_bootstrap(_pod_env(tmp_path))  # no raise


def test_a_signed_out_child_still_boots(tmp_path: Path, resolved) -> None:
    """Seeding is best-effort, so a signed-out host must still get a bootable pod."""
    resolved(
        _fake_cli(
            tmp_path,
            "import sys\n"
            "sys.stderr.write('error: You are not logged in, please log in\\n')\n"
            "raise SystemExit(1)\n",
        )
    )

    rt._probe_pod_child_bootstrap(_pod_env(tmp_path))  # no raise


def test_a_host_without_kiro_cli_is_not_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kiro-cli is a separate prerequisite; its absence has its own message."""
    from kiro_crew.acp import client as acp_client

    monkeypatch.setattr(acp_client, "_resolve_kiro_bin", lambda **_kw: None)

    rt._probe_pod_child_bootstrap(_pod_env(tmp_path))  # no raise


def test_an_unspawnable_child_refuses_rather_than_tracebacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unspawnable binary is a refusal with a reason, not an escaping exception.

    The wrap seam is FAKED, like every other case in this module. Asserting through
    the real one made this test's own subject unreachable on a backend-less runner:
    the sandbox classification fires before the binary is ever exec'd, so the
    refusal arrived as "could not be sandboxed" and the assertion for the
    missing-binary spelling failed. Its subject is the missing-binary path, so the
    sandbox has to be present-and-working for it to be under test at all.
    """
    from kiro_crew import sandbox
    from kiro_crew.acp import client as acp_client

    missing = tmp_path / "not-there" / "kiro-cli"
    monkeypatch.setattr(acp_client, "_resolve_kiro_bin", lambda **_kw: str(missing))
    monkeypatch.setattr(
        acp_client, "apply_pod_bundle_spawn", lambda argv, **_kw: ([str(missing)], False)
    )
    monkeypatch.setattr(
        sandbox, "sandboxed_spawn_argv", lambda argv, *_a, **kw: (list(argv), dict(kw["env"]), None)
    )

    with pytest.raises(rt.PodError) as excinfo:
        rt._probe_pod_child_bootstrap(_pod_env(tmp_path))

    # Either spelling is correct and which fires depends on whether a launcher ran:
    # unwrapped, ``Popen`` raises OSError ("could not be started"); wrapped, the
    # launcher starts, fails to exec the target and exits non-zero, so the refusal
    # arrives through the exit path. The contract owes a PodError that names the
    # child and carries a reason, not one specific spelling.
    message = str(excinfo.value)
    assert "kiro-cli child" in message
    assert ("could not be started" in message) or ("exited immediately" in message)


def test_a_sandbox_refusal_outranks_a_missing_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRECEDENCE, pinned so it cannot flap: sandbox-unavailable wins.

    Both conditions hold at once here -- no sandbox backend AND a binary that
    cannot be exec'd -- and both would refuse the boot, so the precedence decides
    only the RECORDED REASON. Sandbox-unavailable is reported because it is a
    property of the HOST that blocks every spawn regardless of which binary is
    resolved: an operator told to reinstall kiro-cli would fix the named problem
    and still get a dead pod, while an operator told the sandbox has no backend
    fixes the thing that is actually in the way.

    The ordering above it is unchanged and deliberate: a host with NO kiro-cli at
    all is still ``PROBE_UNAVAILABLE`` (skip, not refuse), because that is a
    supported configuration rather than a broken one. Full order --
    unavailable > sandbox-unavailable > dead(binary).
    """
    from kiro_crew import sandbox
    from kiro_crew.acp import client as acp_client
    from kiro_crew.agent_sdk import pod_child_probe as probe_mod

    missing = tmp_path / "not-there" / "kiro-cli"
    monkeypatch.setattr(acp_client, "_resolve_kiro_bin", lambda **_kw: str(missing))
    monkeypatch.setattr(
        acp_client, "apply_pod_bundle_spawn", lambda argv, **_kw: ([str(missing)], False)
    )

    def _no_sandbox(_argv, *_a, **_kw):
        raise sandbox.SandboxUnavailableError(
            "no backend", kind="permanent", detail="unshare(CLONE_NEWUSER) failed"
        )

    monkeypatch.setattr(sandbox, "sandboxed_spawn_argv", _no_sandbox)

    result = probe_mod.probe_pod_child_bootstrap(_pod_env(tmp_path), timeout_secs=10.0)

    assert result.verdict == probe_mod.PROBE_DEAD
    assert "could not be sandboxed" in result.detail
    assert "could not be started" not in result.detail


def test_the_probe_refusal_becomes_a_recorded_terminal_exit(
    cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verified, not assumed: the probe runs inside ``boot``'s guard.

    A ``PodError`` raised from ``_boot_unguarded`` must come back as
    ``EXIT_REFUSED_UNRECOVERABLE`` with the reason readable through
    ``refusal_reason`` -- otherwise a refusing probe would restart-loop the unit
    every few seconds with the cause buried in a traceback.
    """

    def _raise(_cfg: PodConfig, _name: str) -> int:
        raise rt.PodError("child could not bootstrap (probe)")

    monkeypatch.setattr(rt, "_boot_unguarded", _raise)

    code = rt.boot(cfg, "viability-probe-test")

    assert code == EXIT_REFUSED_UNRECOVERABLE
    assert "child could not bootstrap (probe)" in (
        rt.refusal_reason(cfg, "viability-probe-test") or ""
    )


def test_boot_probes_before_declaring_the_pod_up() -> None:
    """The call site is ordered after the env is built and before the gateway runs."""
    source = Path(rt.__file__).read_text()
    probe_at = source.index("_probe_pod_child_bootstrap(pod_env)")
    env_at = source.index("pod_env = build_pod_env(")
    gateway_at = source.index('argv = ["gateway"]')
    assert env_at < probe_at < gateway_at


class TestTheProbeIsConfined:
    """The probe's confinement legs, and what happens when they cannot be built.

    The probe runs on the BOOT path, so an unconfined child holding the gateway's
    own AWS material would be a real exposure: it applies the same legs production
    applies -- ``scrub_agent_subprocess_env`` on the env, ``wrap_argv`` on the argv,
    a cgroup v2 scope and the rlimit preexec.

    Every case here FAKES the wrap seam rather than exercising this host's sandbox.
    That is the writing-tests floor, and it is not hypothetical: asserting through
    the real ``wrap_argv`` made these tests pass on a developer box and fail on
    every CI runner, where ``unshare(CLONE_NEWUSER)`` returns EPERM under the
    AppArmor userns restriction. A unit test must pin the CONTRACT -- that the wrap
    was requested with the right arguments, and that each of its outcomes maps to
    the right verdict -- not the capability of the machine running it.
    """

    @staticmethod
    def _spy_on_spawn(monkeypatch, tmp_path: Path, script: Path) -> dict:
        """Point resolution at a Python fake and capture the wrap + spawn calls."""
        from kiro_crew import sandbox
        from kiro_crew.acp import client as acp_client
        from kiro_crew.agent_sdk import pod_child_probe as probe_mod

        seen: dict = {}
        monkeypatch.setattr(acp_client, "_resolve_kiro_bin", lambda **_kw: sys.executable)
        monkeypatch.setattr(
            acp_client,
            "apply_pod_bundle_spawn",
            lambda argv, **_kw: ([sys.executable, str(script)], True),
        )
        real_popen = probe_mod.subprocess.Popen

        def _spy(argv, **kwargs):
            seen["env"] = dict(kwargs.get("env") or {})
            seen["argv"] = list(argv)
            seen["profile"] = kwargs.get("profile")
            kwargs.pop("profile", None)
            # The fake argv[0] is a marker, not an executable -- run the real fake.
            runnable = [a for a in argv if a not in ("WRAPPED", "SCOPE")]
            return real_popen(runnable, **kwargs)

        # The probe spawns through ``popen_limited`` (limits AFTER exec), so that is
        # the seam to observe -- patching ``subprocess.Popen`` here would miss it.
        monkeypatch.setattr(sandbox, "popen_limited", _spy)
        return seen

    def test_the_spawn_is_prepared_through_the_provider_chokepoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sandbox-OK: routed through ``sandboxed_spawn_argv`` with production's args.

        The probe must not reimplement the spawn contract. Asserting the REQUEST is
        what makes that real: mode, the pre-remapped env, the python-env strip, and
        the delegation decision carried through from ``apply_pod_bundle_spawn``
        rather than re-derived.
        """
        from kiro_crew import sandbox
        from kiro_crew.agent_sdk import pod_child_probe as probe_mod

        script = _fake_cli(tmp_path, "raise SystemExit(0)\n")
        seen = self._spy_on_spawn(monkeypatch, tmp_path, script)
        asked: dict = {}

        def _fake_chokepoint(argv, mode="standard", **kwargs):
            asked["argv"] = list(argv)
            asked["mode"] = mode
            asked.update(kwargs)
            # What config THIS call would read, captured at call time.
            asked["seen_kirocrew_home"] = os.environ.get("KIROCREW_HOME", "")
            return (["WRAPPED", *argv], dict(kwargs["env"]), "cleanup-token")

        monkeypatch.setattr(sandbox, "sandboxed_spawn_argv", _fake_chokepoint)
        env = _pod_env(tmp_path)
        env["KIROCREW_HOME"] = str(tmp_path / "pod-crew-home")
        env["AWS_SECRET_ACCESS_KEY"] = "shhh"

        probe_mod.probe_pod_child_bootstrap(env, timeout_secs=10.0)

        assert asked["strip_python_env"] is True
        assert asked["is_kiro_cli"] is True
        assert asked["env"]["HOME"] == str(tmp_path / "os-home")
        # The wrapped argv is what got spawned, not the bare binary.
        assert seen["argv"][0] == "WRAPPED"

    def test_the_preparation_reads_the_PODS_config_not_the_hosts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The finding, pinned: tier + opt-in must resolve under the POD home.

        Red-first. The probe runs in the process that is about to ``exec`` the pod
        gateway, so its ambient ``KIROCREW_HOME`` is the HOST's while the real spawn
        -- inside the pod gateway process -- reads the POD's. Config resolves through
        that variable at call time, so an operator with ``sandbox_allow_unsandboxed_exec``
        set in HOST config and not in POD config previously got a probe that PASSED
        and an agent that could never spawn: healthy status, dead pod.
        """
        from kiro_crew import sandbox
        from kiro_crew.agent_sdk import pod_child_probe as probe_mod

        script = _fake_cli(tmp_path, "raise SystemExit(0)\n")
        self._spy_on_spawn(monkeypatch, tmp_path, script)
        pod_home = tmp_path / "pod-crew-home"
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "HOST-crew-home"))
        seen_homes: list[str] = []

        def _record_context(argv, mode="standard", **kwargs):
            seen_homes.append(os.environ.get("KIROCREW_HOME", ""))
            return (list(argv), dict(kwargs["env"]), None)

        monkeypatch.setattr(sandbox, "sandboxed_spawn_argv", _record_context)

        def _record_mode_read() -> str:
            # The tier read is the other config-dependent decision on this path.
            seen_homes.append(os.environ.get("KIROCREW_HOME", ""))
            return "standard"

        monkeypatch.setattr(sandbox, "configured_sandbox_mode", _record_mode_read)
        env = _pod_env(tmp_path)
        env["KIROCREW_HOME"] = str(pod_home)

        probe_mod.probe_pod_child_bootstrap(env, timeout_secs=10.0)

        assert seen_homes, "no config-reading call observed"
        assert all(home == str(pod_home) for home in seen_homes), seen_homes
        # And the host's environment is restored afterwards -- the swap is scoped to
        # the preparation, not leaked into the rest of the boot.
        assert os.environ["KIROCREW_HOME"] == str(tmp_path / "HOST-crew-home")

    def test_a_host_that_cannot_build_a_sandbox_is_dead_not_a_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sandbox-UNAVAILABLE: a DEAD verdict naming the detail, never an exception.

        Red-first against the unguarded call: ``SandboxUnavailableError`` escaped the
        probe, ``boot`` exited 1, and the service manager restarted into the same
        failure every few seconds -- the loop the terminal-exit work exists to stop.
        """
        from kiro_crew import sandbox
        from kiro_crew.agent_sdk import pod_child_probe as probe_mod

        script = _fake_cli(tmp_path, "raise SystemExit(0)\n")
        seen = self._spy_on_spawn(monkeypatch, tmp_path, script)

        def _no_sandbox(_argv, **_kwargs):
            raise sandbox.SandboxUnavailableError(
                "no backend",
                kind="permanent",
                detail="unshare(CLONE_NEWUSER) failed with errno 1 (EPERM)",
            )

        monkeypatch.setattr(sandbox, "wrap_argv", _no_sandbox)

        result = probe_mod.probe_pod_child_bootstrap(_pod_env(tmp_path), timeout_secs=10.0)

        assert result.verdict == probe_mod.PROBE_DEAD
        # The recorded reason must name the mechanism AND the kind, because those are
        # the operator's decision inputs (install a backend / opt in, vs retry later).
        assert "could not be sandboxed" in result.detail
        assert "permanent" in result.detail
        assert "EPERM" in result.detail
        # Fail-closed: nothing was spawned unconfined as a fallback.
        assert "argv" not in seen

    def test_an_unsealable_governance_ceiling_is_also_dead(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other sandbox-preparation refusal maps the same way, not to a crash."""
        from kiro_crew import sandbox
        from kiro_crew.agent_sdk import pod_child_probe as probe_mod

        script = _fake_cli(tmp_path, "raise SystemExit(0)\n")
        self._spy_on_spawn(monkeypatch, tmp_path, script)

        def _unsealable(_argv, **_kwargs):
            raise sandbox.SandboxCeilingUnsealable("computer_use.json is not sealable")

        monkeypatch.setattr(sandbox, "wrap_argv", _unsealable)

        result = probe_mod.probe_pod_child_bootstrap(_pod_env(tmp_path), timeout_secs=10.0)

        assert result.verdict == probe_mod.PROBE_DEAD
        assert "not sealable" in result.detail

    def test_the_sandbox_refusal_reaches_a_recorded_terminal_exit(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end: unsandboxable host -> recorded terminal refusal, not exit 1."""
        from kiro_crew import sandbox
        from kiro_crew.agent_sdk import pod_child_probe as probe_mod

        script = _fake_cli(tmp_path, "raise SystemExit(0)\n")
        self._spy_on_spawn(monkeypatch, tmp_path, script)
        monkeypatch.setattr(
            sandbox,
            "wrap_argv",
            lambda _argv, **_kw: (_ for _ in ()).throw(
                sandbox.SandboxUnavailableError("no backend", kind="permanent", detail="EPERM")
            ),
        )
        env = _pod_env(tmp_path)

        def _probe_then_raise(_cfg: PodConfig, _name: str) -> int:
            rt._probe_pod_child_bootstrap(env)
            return 0

        monkeypatch.setattr(rt, "_boot_unguarded", _probe_then_raise)

        code = rt.boot(cfg, "viability-sandbox-test")

        assert code == EXIT_REFUSED_UNRECOVERABLE
        reason = rt.refusal_reason(cfg, "viability-sandbox-test") or ""
        assert "could not be sandboxed" in reason
        assert probe_mod.PROBE_DEAD not in reason  # a reason, not a bare verdict token


class TestTheRefusalNoteIsReadNoFollow:
    """``pod ls`` prints the refusal note, so reading it must not follow a link.

    The WRITE side of this path was pinned rounds ago; the READ stayed by-name, so
    the same planted ``<pod>.refused`` symlink still worked -- in the disclosure
    direction instead of the truncation one. A note pointed at any file the gateway
    could read printed that file's contents under the note's own label.
    """

    @staticmethod
    def _cfg_at(pods: Path) -> PodConfig:
        """The REAL config with its pods dir moved, not a stub.

        A duck-typed fake would type-check as ``Any`` and let a signature change
        pass silently, which is the drift these pod tests keep catching elsewhere.
        """
        return dataclasses.replace(PodConfig.load(), pods_dir=pods)

    def test_a_regular_note_still_prints(self, tmp_path: Path) -> None:
        from kiro_crew import pinned_fs

        note = tmp_path / "pod-a.refused"
        pinned_fs.write_file_pinned(note, "child could not bootstrap\n", what="note")

        assert pinned_fs.read_file_pinned(note, what="note").strip() == (
            "child could not bootstrap"
        )

    def test_a_planted_symlink_note_is_refused_and_never_read(self, tmp_path: Path) -> None:
        """Red-first: a by-name ``read_text`` returned the SECRET here."""
        from kiro_crew import pinned_fs

        secret = tmp_path / "host-credential"
        secret.write_text("SECRET-MATERIAL\n")
        note = tmp_path / "pod-b.refused"
        note.symlink_to(secret)

        with pytest.raises(OSError) as excinfo:
            pinned_fs.read_file_pinned(note, what="pod refusal note")

        message = str(excinfo.value)
        assert "not a regular file" in message
        assert "SECRET-MATERIAL" not in message

    def test_a_fifo_note_is_refused_rather_than_blocking(self, tmp_path: Path) -> None:
        """A non-regular note that is not a link either -- refused, not opened.

        Opening a FIFO for reading BLOCKS until a writer appears, so following this
        one would hang ``pod ls`` rather than leak. The same lstat gate catches both,
        which is why the check is "is a regular file" and not "is not a symlink".
        """
        from kiro_crew import pinned_fs

        if not hasattr(os, "mkfifo"):  # pragma: no cover - POSIX-only shape
            pytest.skip("mkfifo is POSIX-only; the lstat gate itself is not")
        note = tmp_path / "pod-c.refused"
        os.mkfifo(note)

        with pytest.raises(OSError, match="not a regular file"):
            pinned_fs.read_file_pinned(note, what="pod refusal note")

    def test_pod_status_reports_a_refusal_without_printing_the_target(self, tmp_path: Path) -> None:
        """End to end through ``refusal_reason``: the SIGNAL survives, the bytes do not.

        The file existing still means "this boot refused" -- the contract
        ``refusal_reason`` documents -- so a planted note degrades to a stated reason,
        not to a clean verdict and not to the target's contents.
        """
        secret = tmp_path / "host-credential"
        secret.write_text("SECRET-MATERIAL\n")
        pods = tmp_path / "pods"
        pods.mkdir(parents=True)
        (pods / "planted.refused").symlink_to(secret)

        reason = rt.refusal_reason(self._cfg_at(pods), "planted") or ""

        assert reason, "a planted note must still report a refusal"
        assert "not a regular file" in reason
        assert "SECRET-MATERIAL" not in reason

    def test_a_missing_note_is_still_a_clean_boot(self, tmp_path: Path) -> None:
        """The no-note case must stay None, not become a false refusal."""
        pods = tmp_path / "pods"
        pods.mkdir(parents=True)

        assert rt.refusal_reason(self._cfg_at(pods), "never-refused") is None
