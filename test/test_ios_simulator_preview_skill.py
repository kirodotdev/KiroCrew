"""Contract tests for the bundled ``ios-simulator-preview`` skill.

The skill mirrors a booted iOS Simulator into the dashboard's Browser panel. Two
properties are worth locking in, because both broke in practice while it was
being built and neither fails loudly at runtime:

1. **The launcher ships and is discoverable.** The skill is the only bundled
   skill outside ``kirocrew-dev/`` with a ``scripts/`` payload, so a packaging
   glob that stops at ``SKILL.md`` would leave the documented command pointing
   at a file that is not installed.
2. **The launcher's hard-won invariants stay in the code.** Each assertion below
   corresponds to a real failure: a turn-reaped mirror, a 401 from an
   authenticated internal npm registry, an "Incompatible device" from selecting
   a device type the installed runtime rejects, and an unbounded ``simctl`` call
   hanging a turn against a wedged CoreSimulator.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# The behavioral probes below drive the launcher as a real subprocess with
# /bin/sh shims on PATH, and assert on POSIX-only semantics (O_NOFOLLOW,
# process-group signals, symlinks). They cannot run on Windows, and need not:
# the skill requires macOS + Xcode. The source-level assertions in the same
# class still run everywhere, so the invariants stay pinned on every platform.
posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="behavioral probe needs POSIX shell shims and fd semantics; skill is macOS-only",
)

SKILL_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "ios-simulator-preview"
)


@pytest.fixture(scope="module")
def skill_md() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def launcher() -> str:
    return (SKILL_DIR / "scripts" / "sim_mirror.py").read_text(encoding="utf-8")


class TestSkillFilesShip:
    def test_skill_md_and_launcher_both_present(self) -> None:
        assert (SKILL_DIR / "SKILL.md").is_file()
        assert (SKILL_DIR / "scripts" / "sim_mirror.py").is_file()

    def test_frontmatter_declares_name_and_triggers(self, skill_md: str) -> None:
        """Without frontmatter the loader cannot match the skill to a request."""
        assert skill_md.startswith("---")
        head = skill_md.split("---", 2)[1]
        assert "name: ios-simulator-preview" in head
        assert "triggers:" in head
        assert "description:" in head

    def test_documented_launcher_path_resolves_via_skill_dir(self, skill_md: str) -> None:
        """The documented path must honor KIROCREW_HOME (same convention as
        prepare-pr), not hardcode a home that a relocated install won't have."""
        assert '"${KIROCREW_HOME:-$HOME/.kiro/crew}/skills/ios-simulator-preview"' in skill_md
        assert '"$SKILL_DIR/scripts/sim_mirror.py"' in skill_md

    def test_macos_only_is_stated(self, skill_md: str) -> None:
        """Apple's simulator is macOS-only; the skill must say so rather than
        letting a Linux/Windows agent attempt it and fail obscurely."""
        assert "macOS only" in skill_md


class TestLauncherInvariants:
    def test_spawns_detached(self, launcher: str) -> None:
        """A turn-scoped child is reaped at turn end, killing the mirror the
        moment the agent stops talking."""
        assert "start_new_session=True" in launcher

    def test_pins_public_npm_registry(self, launcher: str) -> None:
        """A local npm config may default to an authenticated internal registry,
        which 401s on this public package."""
        assert "--registry=https://registry.npmjs.org" in launcher

    def test_selects_device_type_from_runtime_supported_list(self, launcher: str) -> None:
        """The global devicetypes list contains hardware an older runtime
        rejects with 'Incompatible device'; scope selection to the runtime."""
        assert "supportedDeviceTypes" in launcher

    def test_every_subprocess_call_is_timeout_bounded(self, launcher: str) -> None:
        """A wedged CoreSimulator makes simctl block forever. Every
        subprocess.run must carry a timeout so it returns a diagnosable error
        instead of hanging the caller."""
        import re

        calls = re.findall(r"subprocess\.run\((.*?)\)\n", launcher, re.DOTALL)
        assert calls, "expected subprocess.run call sites in the launcher"
        untimed = [c for c in calls if "timeout" not in c]
        assert not untimed, f"subprocess.run without timeout: {untimed}"

    def test_refuses_non_loopback_mirror_url(self, launcher: str) -> None:
        """The panel can only embed loopback origins, and a mirror bound to a
        routable address would expose the simulator on the LAN."""
        assert "LOOPBACK_HOSTS" in launcher
        for host in ("127.0.0.1", "localhost"):
            assert host in launcher

    def test_stop_is_scoped_to_own_pidfiles(self, launcher: str) -> None:
        """An unscoped `serve-sim --kill` would tear down a mirror another
        session owns."""
        assert "--kill" in launcher
        assert ".pid" in launcher


class TestReviewFindings:
    """Regressions for findings raised in review of this skill.

    Each test names the concrete attack or accident it prevents, so a future
    simplification cannot quietly reopen it.
    """

    def test_device_arg_is_validated_before_path_or_signal_use(self, launcher: str) -> None:
        """`stop --device /tmp/svc` would make ``STATE_DIR / f"{udid}.pid"``
        resolve to ``/tmp/svc.pid`` — an absolute right-hand operand replaces the
        base entirely — letting a caller read, unlink, and signal the process
        group named by an arbitrary file. Reject non-UDIDs first."""
        assert "UDID_RE" in launcher
        assert "def require_udid" in launcher
        assert "require_udid(args.device)" in launcher

    def test_udid_pattern_rejects_traversal_and_accepts_real_udids(self) -> None:
        """Exercise the compiled pattern itself, not just its presence."""
        import re

        src = (SKILL_DIR / "scripts" / "sim_mirror.py").read_text(encoding="utf-8")
        pattern = re.search(r"UDID_RE = re\.compile\(r\"(.+?)\"\)", src)
        assert pattern, "UDID_RE definition not found"
        udid_re = re.compile(pattern.group(1))

        assert udid_re.match("09BE828F-6801-4731-BB13-D4C9747BC358")
        for bad in (
            "/tmp/service",          # absolute path — the reported traversal
            "../../etc/passwd",      # relative escape
            "09be828f-6801-4731-bb13-d4c9747bc358",  # lowercase is not canonical
            "09BE828F",              # truncated
            "",
        ):
            assert not udid_re.match(bad), bad

    def test_pid_ownership_is_verified_before_signaling(self, launcher: str) -> None:
        """A recorded pid is not proof of ownership: after the mirror exits the
        OS may reuse that number, and a blind killpg would terminate whatever
        unrelated process group now holds it."""
        assert "def _owns_pid" in launcher
        assert "_owns_pid(pid, udid)" in launcher
        # Identity is re-derived from the live process, not trusted from the file.
        assert '"ps", "-p"' in launcher
        assert "serve-sim" in launcher

    def test_serve_sim_version_is_pinned(self, launcher: str) -> None:
        """`serve-sim@latest` would fetch and execute whatever npm currently
        serves on every start, so a hijacked or broken release changes behavior
        with no diff. Bumping the pin must be a reviewable change."""
        assert "serve-sim@latest" not in launcher
        assert "SERVE_SIM_VERSION" in launcher
        import re

        pin = re.search(r'SERVE_SIM_VERSION = "([^"]+)"', launcher)
        assert pin, "SERVE_SIM_VERSION not found"
        assert re.fullmatch(r"\d+\.\d+\.\d+", pin.group(1)), pin.group(1)

    def test_state_dir_honors_kirocrew_home(self, launcher: str) -> None:
        """Hardcoding ~/.kiro/crew makes a dev instance
        (KIROCREW_HOME=~/.kirocrew-dev) share pidfile state with a production
        install, so one's `stop` reaches into the other's mirrors."""
        assert 'os.environ.get("KIROCREW_HOME")' in launcher
        assert "STATE_DIR = _HOME" in launcher

    def test_subprocess_timeout_yields_json_error_not_traceback(self, launcher: str) -> None:
        """A wedged CoreSimulator makes simctl block to the deadline. This tool's
        whole contract is JSON on stdout or an error object on stderr, so an
        escaping TimeoutExpired would hand the calling agent an unparseable
        traceback exactly when it most needs to be told what to fix."""
        assert "except subprocess.TimeoutExpired:" in launcher
        assert "def sh_best_effort" in launcher
        # die() must be NoReturn, else sh()'s typed return falls through.
        assert "def die(msg: str) -> NoReturn:" in launcher

    @posix_only
    def test_timeout_produces_parseable_json_at_runtime(self, tmp_path: Path) -> None:
        """Exercise the real failure, not just its source text: a command that
        outlives its timeout must exit 1 with a JSON error object."""
        import json
        import subprocess

        script = (SKILL_DIR / "scripts" / "sim_mirror.py").read_text(encoding="utf-8")
        probe = tmp_path / "probe.py"
        probe.write_text(
            script.replace(
                "def main() -> None:",
                "def _probe() -> None:\n    sh(['sleep', '30'], timeout=1)\n\n\n"
                "def main() -> None:",
            ).replace(
                'if __name__ == "__main__":\n    main()',
                'if __name__ == "__main__":\n    _probe()',
            ),
            encoding="utf-8",
        )
        p = subprocess.run(
            [sys.executable, str(probe)], capture_output=True, text=True, timeout=60
        )
        assert p.returncode == 1
        assert "Traceback" not in p.stderr
        payload = json.loads(p.stderr.strip())
        assert "timed out after 1s" in payload["error"]

    def test_vendor_cleanup_is_gated_on_local_ownership(self, launcher: str) -> None:
        """The vendor `--kill` flag addresses a mirror by UDID alone and cannot
        tell whose it is. Run unconditionally, a `stop` (or a `start`) in one
        KIROCREW_HOME would tear down a live mirror another home owns for the
        same device."""
        assert "subprocess.run(NPX" not in launcher, "ungated vendor cleanup call"
        assert '"tracked": tracked' in launcher

    def test_ownership_requires_a_live_process_not_just_a_pidfile(self, launcher: str) -> None:
        """A *stale* pidfile is not ownership. Treating it as ownership let this
        home's cleanup reach a live mirror another home legitimately owned for
        the same device — the pidfile-existence gate was too weak."""
        assert "def _live_owned_pid" in launcher
        # Both cleanup call sites must consult live ownership.
        assert "_live_owned_pid(udid) is not None" in launcher
        assert "pid = _live_owned_pid(udid)" in launcher
        # ...and the weak gate must be gone.
        assert '(STATE_DIR / f"{udid}.pid").exists():\n        sh_best_effort' not in launcher

    @posix_only
    def test_stale_pidfile_triggers_no_vendor_cleanup_at_runtime(self, tmp_path: Path) -> None:
        """Behavioral proof: with a pidfile naming a dead pid, `stop` must drop
        the file and invoke NOTHING external — no vendor cleanup that could hit
        another home's mirror."""
        import json
        import subprocess

        home = tmp_path / "home"
        state = home / "workspace" / "sim-mirror"
        state.mkdir(parents=True)
        udid = "09BE828F-6801-4731-BB13-D4C9747BC358"
        # A pid that cannot be ours: spawn a real child, wait for it to exit, and
        # reuse its number — reaped, so nothing of ours holds it.
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=30)
        (state / f"{udid}.pid").write_text(str(dead.pid))

        # Shim PATH so any npx/ps/xcrun invocation is recorded rather than real.
        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        calls = tmp_path / "calls.log"
        for name in ("npx", "xcrun"):
            shim = shim_dir / name
            shim.write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls}"\nexit 0\n')
            shim.chmod(0o755)

        env = {
            **os.environ,
            "KIROCREW_HOME": str(home),
            "PATH": f"{shim_dir}:{os.environ.get('PATH', '')}",
        }
        p = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "sim_mirror.py"),
             "stop", "--device", udid],
            capture_output=True, text=True, timeout=120, env=env,
        )
        assert p.returncode == 0, p.stderr
        result = json.loads(p.stdout)["stopped"][0]
        assert result["tracked"] is True
        assert result["killed"] is False
        assert not (state / f"{udid}.pid").exists(), "stale pidfile should be dropped"
        assert not calls.exists(), f"stale pidfile must not invoke: {calls.read_text() if calls.exists() else ''}"

    def test_mirror_spawn_survives_a_missing_npx(self, launcher: str) -> None:
        """Popen bypasses sh()'s conversion, so a missing npx (no Node) would
        surface as a traceback instead of the JSON error the caller parses."""
        spawn = launcher.split("subprocess.Popen(", 1)
        assert len(spawn) == 2, "Popen call not found"
        assert "except OSError as exc:" in spawn[1].split("pidfile.write_text", 1)[0]

    def test_simctl_json_output_is_never_parsed_unchecked(self, launcher: str) -> None:
        """simctl can exit nonzero or print a non-JSON diagnostic when
        CoreSimulator is wedged or a platform is missing. Parsing that blind
        raised JSONDecodeError, discarding the real reason (simctl's stderr)."""
        assert "def simctl_json" in launcher
        assert "json.JSONDecodeError" in launcher
        # No caller may parse simctl stdout directly any more.
        assert "json.loads(simctl(" not in launcher
        assert "json.loads(out.stdout" not in launcher

    @posix_only
    def test_wedged_simctl_reports_json_error_at_runtime(self, tmp_path: Path) -> None:
        """Behavioral proof: with an `xcrun` that fails the way a wedged
        CoreSimulator does, `start` must exit 1 with a parseable JSON error
        carrying simctl's own diagnostic — not a traceback."""
        import json
        import subprocess

        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        xcrun = shim_dir / "xcrun"
        xcrun.write_text(
            '#!/bin/sh\n'
            'echo "Failed to open a connection to CoreSimulatorService" >&2\n'
            'exit 164\n'
        )
        xcrun.chmod(0o755)

        env = {
            **os.environ,
            "KIROCREW_HOME": str(tmp_path / "home"),
            "PATH": f"{shim_dir}:{os.environ.get('PATH', '')}",
        }
        p = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "sim_mirror.py"), "start"],
            capture_output=True, text=True, timeout=120, env=env,
        )
        assert p.returncode == 1
        assert "Traceback" not in p.stderr, p.stderr
        payload = json.loads(p.stderr.strip())
        assert "rc=164" in payload["error"]
        assert "CoreSimulatorService" in payload["error"]

    def test_status_tolerates_a_pidfile_vanishing_mid_read(self, launcher: str) -> None:
        """A concurrent `stop` can unlink a pidfile between the glob and the
        read. status is a read-only snapshot and must not crash on that race."""
        status_body = launcher.split("def cmd_status", 1)[1]
        assert "except (ValueError, OSError):" in status_body

    def test_log_open_refuses_to_follow_a_symlink(self, launcher: str) -> None:
        """The log path is predictable, so a symlink planted there would be
        followed by a plain "w" open and its target truncated."""
        assert "os.O_NOFOLLOW" in launcher
        assert 'open(log, "w")' not in launcher
        assert "0o600" in launcher  # log stays private

    def test_relative_data_home_is_refused_not_silently_resolved(self, launcher: str) -> None:
        """The launcher is invoked by path from whatever directory the session is
        in, so a relative KIROCREW_HOME would resolve per-caller: a `start` from
        one project directory would be invisible to a `stop` from another."""
        assert "def _resolve_home" in launcher
        assert ".expanduser()" in launcher          # env vars carry ~ unexpanded
        assert "is_absolute()" in launcher
        # die() must precede the import-time home resolution, or the error path
        # raises NameError instead of reporting.
        assert launcher.index("def die(") < launcher.index("_HOME = _resolve_home()")

    @posix_only
    def test_relative_data_home_errors_at_runtime(self, tmp_path: Path) -> None:
        """Behavioral proof: a relative home exits 1 with a JSON error naming the
        offending value, from a cwd where that relative path exists."""
        import json
        import subprocess

        (tmp_path / "relhome").mkdir()
        p = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "sim_mirror.py"), "status"],
            capture_output=True, text=True, timeout=60, cwd=tmp_path,
            env={**os.environ, "KIROCREW_HOME": "relhome"},
        )
        assert p.returncode == 1
        assert "Traceback" not in p.stderr, p.stderr
        error = json.loads(p.stderr.strip())["error"]
        assert "absolute" in error and "relhome" in error

    @posix_only
    def test_tilde_data_home_is_expanded_at_runtime(self, tmp_path: Path) -> None:
        """`~/...` is a legitimate value and must be accepted, not refused."""
        import json
        import subprocess

        fake_home = tmp_path / "fakehome"
        (fake_home / "crewhome" / "workspace" / "sim-mirror").mkdir(parents=True)
        p = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "sim_mirror.py"), "status"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "HOME": str(fake_home), "KIROCREW_HOME": "~/crewhome"},
        )
        assert p.returncode == 0, p.stderr
        assert json.loads(p.stdout) == {"mirrors": []}

    @posix_only
    def test_symlinked_log_is_not_written_through_at_runtime(self, tmp_path: Path) -> None:
        """Behavioral proof: plant a symlink where the mirror log goes and assert
        `start` refuses with a JSON error and leaves the target byte-identical."""
        import json
        import subprocess

        home = tmp_path / "home"
        state = home / "workspace" / "sim-mirror"
        state.mkdir(parents=True)
        udid = "09BE828F-6801-4731-BB13-D4C9747BC358"

        victim = tmp_path / "victim.txt"
        victim.write_text("do not truncate me")
        (state / f"{udid}.log").symlink_to(victim)

        # Shim simctl so device resolution reports this udid already booted, and
        # npx so nothing real is ever launched if the guard were to fail.
        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        devices = json.dumps({"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-18-0": [
            {"udid": udid, "name": "iPhone Probe", "state": "Booted", "isAvailable": True}
        ]}})
        xcrun = shim_dir / "xcrun"
        xcrun.write_text(f"#!/bin/sh\ncat <<'JSON'\n{devices}\nJSON\nexit 0\n")
        xcrun.chmod(0o755)
        npx = shim_dir / "npx"
        npx.write_text('#!/bin/sh\nexit 0\n')
        npx.chmod(0o755)

        env = {
            **os.environ,
            "KIROCREW_HOME": str(home),
            "PATH": f"{shim_dir}:{os.environ.get('PATH', '')}",
        }
        p = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "sim_mirror.py"),
             "start", "--device", udid],
            capture_output=True, text=True, timeout=120, env=env,
        )
        assert p.returncode == 1
        assert "Traceback" not in p.stderr, p.stderr
        assert "refusing to open the mirror log" in json.loads(p.stderr.strip())["error"]
        assert victim.read_text() == "do not truncate me", "symlink target was written through"
