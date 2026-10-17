"""A cron script child whose filesystem returns ENOSYS must fail loudly.

A detached script child inherits its parent's seccomp filter, and seccomp
survives the sandbox that installed it: when that sandbox is torn down underneath
the child, every basic file syscall returns ``ENOSYS`` (errno 38) while the
process keeps running. Before this change the child reached user code, the user
function returned, the launcher printed ``{"status": "ok"}`` and the parent
recorded a successful run for a job that persisted nothing -- the reported
incident pinned a downstream watermark for 13 days with no error anywhere.

Pinned here: ``cron_script.child_persistence_preflight`` runs in the launcher
preamble, before the script body, and exits non-zero with the probe's own
diagnosis; and the child carries the env marker the sandbox's audit guard reads.

Builtin fixtures only, so the module runs under ``--noconftest`` too.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import cron_script
from kiro_crew.cron_script import (
    CHILD_PERSISTENCE_EXIT_CODE,
    CHILD_PERSISTENCE_PREFIX,
    child_persistence_preflight,
    run_script_sandboxed,
)
from kiro_crew.sandbox import CRON_SCRIPT_CHILD_ENV

#: What the probe returns on a torn-down sandbox, shortened. The seccomp clause
#: is the operator-actionable half, so the tests assert it survives to stderr
#: rather than just "some message".
ENOSYS_PROBE_MESSAGE = (
    "cannot lock files in /home/u/.kiro/crew: [Errno 38] Function not implemented "
    "(ENOSYS from a basic file syscall usually means this process inherited a "
    "seccomp filter from a sandboxed parent)"
)


@pytest.fixture
def cron_home(tmp_path, monkeypatch):
    """Point ``config_dir()`` at a test-owned home with a ``crons/`` dir.

    ``resolve_script_path`` admits a script only under ``config_dir()/crons``,
    and ``config_dir()`` resolves from ``KIROCREW_HOME``.
    """
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    crons = home / "crons"
    crons.mkdir()
    return crons


def _write_cron_script(crons_dir: Path, code: str) -> str:
    script = crons_dir / "enosys_probe.py"
    script.write_text(code, newline="\n", encoding="utf-8")
    return str(script)


_STOP = "stop before spawn"


def _capture_launcher(script_path: str) -> str:
    """Return the launcher source ``run_script_sandboxed`` would have spawned.

    Intercepts at ``wrap_argv`` -- the last seam before the spawn -- so the
    preamble under test is the real one this call built.
    """
    captured: dict[str, str] = {}

    def _capture(argv, **kwargs):
        captured["launcher"] = Path(argv[1]).read_text(encoding="utf-8")
        raise RuntimeError(_STOP)

    with patch("kiro_crew.cron_script.wrap_argv", _capture):
        with pytest.raises(RuntimeError, match=_STOP):
            run_script_sandboxed(script_path + ":run", "job-id")

    assert "launcher" in captured, "wrap_argv was never reached"
    return captured["launcher"]


class TestLauncherRunsThePreflight:
    """The probe must be IN the preamble, and ahead of the script body.

    String-level, because ordering is the whole property: a probe that runs after
    the body has executed cannot stop a no-op run from reporting success.
    """

    def _launcher(self, crons_dir) -> str:
        script_path = _write_cron_script(crons_dir, "def run(ctx):\n    pass\n")
        return _capture_launcher(script_path)

    def test_preamble_calls_the_preflight(self, cron_home):
        launcher = self._launcher(cron_home)
        assert "from kiro_crew.cron_script import child_persistence_preflight" in launcher
        assert "child_persistence_preflight()" in launcher

    def test_preflight_runs_after_boot_and_before_the_script_body(self, cron_home):
        launcher = self._launcher(cron_home)
        boot = launcher.index("boot_platform(KiroCrewConfig.load())")
        preflight = launcher.index("child_persistence_preflight()")
        body = launcher.index("exec(compile(")
        assert boot < preflight < body, (
            "the preflight must run after boot_platform and before the script "
            f"body is executed (boot={boot}, preflight={preflight}, body={body})"
        )


class TestChildEnvCarriesTheMarker:
    """The sandbox audit guard keys on an env marker the launcher sets.

    Without it the audit sites stay best-effort everywhere and half of the fix is
    inert, so the marker is asserted on the env actually handed to the spawn.
    """

    def test_marker_is_set_on_the_child_env(self, cron_home, monkeypatch):
        monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None))
        captured: dict[str, dict[str, str]] = {}

        def _capture_popen(argv, **kwargs):
            captured["env"] = kwargs["env"]
            raise RuntimeError(_STOP)

        monkeypatch.setattr("kiro_crew.cron_script.popen_limited", _capture_popen)
        script_path = _write_cron_script(cron_home, "def run(ctx):\n    pass\n")
        with pytest.raises(RuntimeError, match=_STOP):
            run_script_sandboxed(script_path + ":run", "job-id")

        assert captured["env"].get(CRON_SCRIPT_CHILD_ENV) == "1"

    def test_the_parent_itself_is_never_marked(self, cron_home, monkeypatch):
        """The gateway must not inherit the marker from running a job."""
        monkeypatch.delenv(CRON_SCRIPT_CHILD_ENV, raising=False)
        monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None))
        monkeypatch.setattr(
            "kiro_crew.cron_script.popen_limited",
            lambda argv, **k: (_ for _ in ()).throw(RuntimeError(_STOP)),
        )
        script_path = _write_cron_script(cron_home, "def run(ctx):\n    pass\n")
        with pytest.raises(RuntimeError, match=_STOP):
            run_script_sandboxed(script_path + ":run", "job-id")

        assert CRON_SCRIPT_CHILD_ENV not in os.environ


class TestPreflightVerdict:
    """What the preflight does with each probe answer."""

    def test_failed_probe_exits_non_zero_with_the_message(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "kiro_crew.platform_compat.probe_file_persistence",
            lambda directory: ENOSYS_PROBE_MESSAGE,
        )
        with pytest.raises(SystemExit) as exit_info:
            child_persistence_preflight()

        assert exit_info.value.code == CHILD_PERSISTENCE_EXIT_CODE
        assert exit_info.value.code != 0
        err = capsys.readouterr().err
        assert CHILD_PERSISTENCE_PREFIX in err
        assert "inherited a seccomp filter" in err, (
            "the probe's own diagnosis is the only actionable part of the "
            f"failure an operator sees. Got: {err!r}"
        )

    def test_passing_probe_is_silent_and_returns(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "kiro_crew.platform_compat.probe_file_persistence", lambda directory: None
        )
        assert child_persistence_preflight() is None
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""

    def test_probe_reads_the_data_home(self, monkeypatch):
        """The probed directory must be the one the child persists into."""
        seen: list[Path] = []
        monkeypatch.setattr(
            "kiro_crew.platform_compat.probe_file_persistence",
            lambda directory: seen.append(directory) or None,
        )
        child_persistence_preflight()
        from kiro_crew.config.paths import data_home

        assert seen == [data_home()]


class TestChildFailsLoudEndToEnd:
    """Through the REAL launcher, on every platform.

    ``wrap_argv`` fails closed on a host with no OS sandbox backend (every CI
    runner here), so the wrap is bypassed and ``src/`` is put on the child's
    ``PYTHONPATH`` -- the same seam ``test_cron_child_boot_platform.py`` uses.

    A child's data home is made unusable by pointing it at a path where an
    ordinary FILE sits: resolving the home creates it, so it fails on every
    platform with no permission bits involved, which a read-only directory cannot
    do (Windows does not deny creation in one, and root ignores the bits).
    """

    @pytest.fixture
    def spawns_real_child(self, monkeypatch, tmp_path):
        src_dir = str(Path(__file__).resolve().parents[1] / "src")
        existing = os.environ.get("PYTHONPATH", "")
        monkeypatch.setenv("PYTHONPATH", src_dir + (os.pathsep + existing if existing else ""))
        monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None))
        # The spawned children inherit this process's CWD, so move it out of the
        # checkout: anything one of them writes to a relative path lands in the
        # repository otherwise. Every path these tests pass is absolute.
        monkeypatch.chdir(tmp_path)

    @staticmethod
    def _child_home(monkeypatch, home: Path) -> None:
        """Point the CHILD's ``KIROCREW_HOME`` at *home*, leaving the parent's.

        Setting it on the test process would move ``config_dir()`` for the parent
        too, and the script the parent admits lives under the parent's own home.
        """
        real = cron_script._clean_cron_env

        def _with_home() -> dict[str, str]:
            env = real()
            env["KIROCREW_HOME"] = str(home)
            return env

        monkeypatch.setattr(cron_script, "_clean_cron_env", _with_home)

    def test_a_child_with_an_unusable_data_home_never_runs_user_code(
        self, cron_home, spawns_real_child, monkeypatch, tmp_path
    ):
        """A child broken before its first line must not reach the script body.

        Deliberately not asserting WHICH preamble step refuses: with the home
        already unusable, the config load that precedes the preflight resolves the
        same directory and can be the one that dies. Either way the job is an
        error and the script body never ran. The preflight's own verdict is pinned
        by ``TestPreflightVerdict`` and its position by the ordering test.
        """
        blocked = tmp_path / "blocked-home"
        blocked.write_text("not a directory", encoding="utf-8")
        sentinel = tmp_path / "user-code-ran"
        script_path = _write_cron_script(
            cron_home,
            "def run(ctx):\n" f"    open({str(sentinel)!r}, 'w').close()\n",
        )
        self._child_home(monkeypatch, blocked)
        result = run_script_sandboxed(script_path + ":run", "job-id", timeout=120)

        assert result["status"] == "error", (
            "a child that cannot persist anything must be recorded as a failed "
            f"run, not as ok. Got: {result!r}"
        )
        assert (
            not sentinel.exists()
        ), "user code ran inside a child whose data home cannot be used at all"

    def test_a_script_catching_exception_cannot_swallow_the_refusal(
        self, cron_home, spawns_real_child
    ):
        """The audit refusal is raised inside the user function's own stack.

        ``ctx.call_tool`` re-enters ``wrap_argv`` to spawn its MCP server, so a
        cron script that wraps its work in ``try: ... except Exception:`` -- an
        ordinary defensive habit -- must not be able to turn the refusal into a
        normal return and a ``{"status": "ok"}`` envelope on exit 0.
        """
        script_path = _write_cron_script(
            cron_home,
            "from kiro_crew.sandbox import UnauditedSpawnRefused\n"
            "def run(ctx):\n"
            "    try:\n"
            "        raise UnauditedSpawnRefused('audit write failed with ENOSYS')\n"
            "    except Exception:\n"
            "        pass\n",
        )
        result = run_script_sandboxed(script_path + ":run", "job-id", timeout=120)

        assert result["status"] == "error", (
            "a script's own except Exception swallowed the audit refusal, so the "
            f"child reported a successful no-op run. Got: {result!r}"
        )
        assert (
            "ENOSYS" in result["error"]
        ), f"the refusal's message must reach the operator. Got: {result['error'][:400]!r}"

    def test_healthy_child_is_unaffected(self, cron_home, spawns_real_child):
        """Control: a child with a working data home runs and reports normally."""
        script_path = _write_cron_script(
            cron_home, "def run(ctx):\n    print('side effect', flush=True)\n"
        )
        result = run_script_sandboxed(script_path + ":run", "job-id", timeout=120)
        assert result == {"status": "ok"}, f"healthy run changed shape: {result!r}"
