"""Tests for the boot_platform call in the cron child launcher preamble.

Script crons execute in a fresh interpreter built by the launcher preamble in
``cron_script.run_script_sandboxed``. The preamble never installed a
``PlatformContext``, so under a non-standalone (companion/enterprise) profile the
child's first platform-aware operation reached ``current_context()`` with a
non-standalone profile and no installed context, and failed closed (issue #6431).

Every test here drives the REAL launcher through ``run_script_sandboxed``. An
earlier revision of this file spawned hand-written child scripts that called
``boot_platform`` themselves; those re-proved library behavior already pinned by
``test_platform_context.py`` / ``test_cpp_wiring_standalone.py`` /
``test_security.py`` and passed with the production change reverted, so they
guarded nothing. The two cases below are mutation-verified against the three
preamble lines this module exists to protect.

Must be runnable with ``--noconftest`` (no hypothesis dependency).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.cron_script import run_script_sandboxed
from kiro_crew.platform import PROFILE_ENTERPRISE


@pytest.fixture(autouse=True)
def _cron_home(tmp_path, monkeypatch):
    """Point ``config_dir()`` at a test-owned home.

    ``resolve_script_path`` admits a script only under ``config_dir()/crons``,
    and ``config_dir()`` resolves from ``KIROCREW_HOME`` -- not from
    ``Path.home`` -- so overriding the env var is what actually moves the allowed
    directory. Doing it here (rather than relying on the repo conftest's own home)
    also keeps the module honest under ``--noconftest``, where no home override
    exists and a bare ``config_dir()`` would be the developer's real ``~/.kiro``.
    """
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    crons = home / "crons"
    crons.mkdir()
    return crons


def _write_cron_script(crons_dir: Path, code: str) -> str:
    script = crons_dir / "boot_probe.py"
    script.write_text(code, newline="\n")
    return str(script)


@pytest.fixture
def _spawns_real_child(monkeypatch):
    """Let the two end-to-end cases actually spawn, on any host.

    ``wrap_argv`` fails closed when the host has no OS sandbox backend -- true of
    every CI runner here (Linux runners forbid unprivileged ``unshare``, Windows
    and macOS 26 have no backend at all). Without this the call returns the
    sandbox-unavailable error and no child is ever launched, so a test asserting
    on child behavior would pass or fail on host configuration rather than on the
    preamble. Mirrors ``test_cron_script.py``'s ``_passthrough_sandbox``: bypass
    the wrap, and put ``src/`` on ``PYTHONPATH`` so the fresh interpreter can
    import ``kiro_crew`` when the checkout is not a packaged install.
    """
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", src_dir + (os.pathsep + existing if existing else ""))
    monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None))


_STOP = "stop before spawn"


def _capture_launcher(script_path: str) -> str:
    """Return the launcher source ``run_script_sandboxed`` would have spawned.

    Intercepts at ``wrap_argv`` -- the last seam before the spawn -- so the
    preamble under test is the real one this call built, not a copy. The stub
    raises to stop short of actually running a child; ``run_script_sandboxed``
    only special-cases ``SandboxUnavailableError``, so this one propagates and is
    swallowed here.
    """
    captured: dict[str, str] = {}

    def _capture(argv, **kwargs):
        captured["launcher"] = Path(argv[1]).read_text()
        raise RuntimeError(_STOP)

    with patch("kiro_crew.cron_script.wrap_argv", _capture):
        with pytest.raises(RuntimeError, match=_STOP):
            run_script_sandboxed(script_path + ":run", "job-id")

    assert "launcher" in captured, "wrap_argv was never reached"
    return captured["launcher"]


def _child_profile(monkeypatch, profile: str) -> None:
    """Resolve ``profile`` in the CHILD only.

    Setting ``KIROCREW_PROFILE`` on the test process itself would make the
    PARENT's own ``current_context()`` fail closed long before it spawns
    anything, so the override goes on the child env the launcher builds.
    """
    from kiro_crew import cron_script

    real = cron_script._clean_cron_env

    def _with_profile() -> dict[str, str]:
        env = real()
        env["KIROCREW_PROFILE"] = profile
        return env

    monkeypatch.setattr(cron_script, "_clean_cron_env", _with_profile)


class TestLauncherPreambleOrdering:
    """The boot must be in the launcher, and ordered against its neighbours.

    A string-level check because ordering is the whole property: booting after
    the ``ScriptContext`` import would leave the cron module's own import-time
    platform touches unbooted, and booting before the ``sys.path`` strip would
    let a stray sibling module in the launcher's temp dir shadow the stdlib for
    the boot itself -- the failure the strip exists to prevent.
    """

    def _launcher(self, crons_dir) -> str:
        script_path = _write_cron_script(crons_dir, "def run(ctx):\n    pass\n")
        return _capture_launcher(script_path)

    def test_launcher_boots_the_platform(self, _cron_home):
        launcher = self._launcher(_cron_home)
        assert "from kiro_crew.platform.bootstrap import boot_platform" in launcher
        assert "boot_platform(KiroCrewConfig.load())" in launcher

    def test_boot_is_after_syspath_strip_and_before_scriptcontext(self, _cron_home):
        launcher = self._launcher(_cron_home)
        strip = launcher.index("sys.path[:] = [p for p in sys.path")
        boot = launcher.index("boot_platform(KiroCrewConfig.load())")
        script_context = launcher.index("from kiro_crew.cron_script import ScriptContext")
        assert strip < boot < script_context, (
            "boot_platform must run after the sys.path strip and before the "
            f"ScriptContext import (strip={strip}, boot={boot}, ctx={script_context})"
        )


class TestChildArrivesBooted:
    """End-to-end through the launcher: the child must arrive BOOTED.

    ``current_context()`` lazily composes an all-defaults standalone context on
    first touch, so on a standalone host the child works either way -- which is
    exactly why the bug survived. The discriminator is not whether a context
    exists but whether it was BOOTED: ``_BOOTED`` is set only by a real
    ``boot_platform``, never by the lazy default.
    """

    PROBE = (
        "import json\n"
        "def run(ctx):\n"
        "    from kiro_crew.platform import bootstrap\n"
        "    from kiro_crew.platform.context import current_context\n"
        "    raise SystemExit(\n"
        "        json.dumps(\n"
        "            {'booted': bootstrap._BOOTED, 'profile': current_context().profile}\n"
        "        )\n"
        "    )\n"
    )

    def test_user_code_sees_a_booted_platform(self, _cron_home, _spawns_real_child):
        script_path = _write_cron_script(_cron_home, self.PROBE)
        result = run_script_sandboxed(script_path + ":run", "job-id", timeout=60)

        # SystemExit(str) exits non-zero with the payload on stderr, which is the
        # one channel the launcher does not reshape into its own JSON envelope.
        assert result["status"] == "error"
        payload = json.loads(result["error"].strip().splitlines()[-1])
        assert payload["booted"] is True, (
            "the cron child reached user code with an un-booted platform: the "
            "context was lazily defaulted, not composed by boot_platform"
        )
        assert payload["profile"] == "standalone"


class TestNonStandaloneFailsAtLaunchNotMidScript:
    """The issue's actual shape: WHERE a non-standalone child fails.

    Without the boot, a companion/enterprise child starts fine and dies at the
    first platform-aware call inside user code -- so the launcher catches it,
    exits 0, and the operator reads a cron failure attributed to their script.
    With the boot, composition is decided in the preamble, before user code
    exists. This host has no companion installed, so enterprise composition
    fails closed either way; what the fix changes is WHERE.

    The sentinel is a FILE, not a stderr line, on purpose: without the fix the
    child exits 0 and its stderr is discarded, so a stderr-based sentinel would
    be absent in both directions and the assertion would pass against the bug.
    """

    def test_boot_failure_precedes_user_code(
        self, _cron_home, _spawns_real_child, monkeypatch, tmp_path
    ):
        _child_profile(monkeypatch, PROFILE_ENTERPRISE)
        sentinel = tmp_path / "user-code-ran"
        script_path = _write_cron_script(
            _cron_home,
            "def run(ctx):\n"
            f"    open({str(sentinel)!r}, 'w').close()\n"
            "    from kiro_crew.platform.context import current_context\n"
            "    current_context()\n",
        )
        result = run_script_sandboxed(script_path + ":run", "job-id", timeout=60)

        assert result["status"] == "error"
        assert not sentinel.exists(), (
            "user code ran before the platform was composed -- the child reached "
            "the script body and only then failed closed, which is the "
            "misattributed-failure shape #6431 describes"
        )
        assert "PlatformCompositionError" in result["error"], (
            "expected the preamble's fail-closed traceback; the class name only "
            f"reaches the operator via stderr. Got: {result['error'][:300]!r}"
        )


@pytest.mark.parametrize("profile", ["standalone", PROFILE_ENTERPRISE])
def test_launcher_is_profile_independent(_cron_home, monkeypatch, profile):
    """The preamble boots unconditionally -- the profile only decides the outcome.

    Guards against a future 'only boot when it looks necessary' narrowing, which
    would reintroduce the bug for every profile the predicate failed to name.
    """
    _child_profile(monkeypatch, profile)
    script_path = _write_cron_script(_cron_home, "def run(ctx):\n    pass\n")
    assert "boot_platform(KiroCrewConfig.load())" in _capture_launcher(script_path)
