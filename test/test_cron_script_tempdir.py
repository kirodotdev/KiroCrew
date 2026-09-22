"""A script cron must run when the temp dir its process inherited has vanished.

``tempfile`` seeds a process-wide default from ``TMPDIR`` once and keeps it.
The gateway can inherit a per-process ``agent_scratch`` directory as that
value from whichever agent session started it; the hourly sweep reclaims that
directory once its recorded owner is dead and the tree has been idle for an
hour, which a daily job's few-second touch guarantees. Every temp file the
run then creates with ``dir=None`` fails with ``ENOENT`` on the missing
parent, and the job is skipped until the gateway restarts.

These tests plant exactly that state -- a cached default naming a directory
that has since been deleted -- and drive the real launcher path through it.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.cron_script import (
    _clean_cron_env,
    _default_temp_dir,
    compute_secret_env_pin,
    run_script_sandboxed,
)
from kiro_crew.secrets import SecretVault

_TEMP_KEYS = ("TMPDIR", "TMP", "TEMP")


@pytest.fixture(autouse=True)
def _cron_caller_is_named(named_cron_caller):
    """These tests are about temp-dir handling, not caller authorization."""


@pytest.fixture(autouse=True)
def _cron_home(monkeypatch):
    """Scripts live under ``<patched home>/.kirocrew/crons``, like the sibling modules."""
    monkeypatch.setattr("kiro_crew.cron_script.config_dir", lambda: Path.home() / ".kirocrew")


@pytest.fixture(autouse=True)
def _passthrough_sandbox(monkeypatch):
    """Bypass the OS-sandbox wrap and let the child import ``kiro_crew``."""
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", src_dir + (os.pathsep + existing if existing else ""))
    monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None))
    monkeypatch.setattr("kiro_crew.cron_script._resolve_command_shell", lambda: "sh")


@pytest.fixture
def live_tempdir(tmp_path) -> Path:
    """Where re-resolution must land: a later env candidate, under ``tmp_path``.

    ``tempfile`` probes ``TMPDIR``, ``TEMP``, ``TMP`` in that order before the
    platform defaults. Keeping ``TEMP`` on a live directory inside the test's
    own root means the fallback never reaches the host's shared temp dir, so a
    run killed mid-way leaves nothing outside ``tmp_path``.
    """
    live = tmp_path / "live-temp"
    live.mkdir()
    return live


@pytest.fixture
def vanished_tempdir(tmp_path, monkeypatch, live_tempdir) -> Path:
    """The inherited temp dir: named by the env AND by tempfile's cache, then gone.

    Mirrors the production sequence exactly -- the env var and the cache are
    both snapshots taken while the directory existed; the sweep removes the
    directory afterwards and neither snapshot is told.
    """
    gone = tmp_path / "scratch" / "runtime-dead0000"
    gone.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(gone))
    monkeypatch.setenv("TMP", str(gone))
    monkeypatch.setenv("TEMP", str(live_tempdir))
    monkeypatch.setattr(tempfile, "tempdir", str(gone))
    assert tempfile.gettempdir() == str(gone)
    shutil.rmtree(gone)
    return gone


def _make_script(tmp_path: Path, body: str) -> Path:
    crons_dir = tmp_path / ".kirocrew" / "crons"
    crons_dir.mkdir(parents=True, exist_ok=True)
    script = crons_dir / "job.py"
    script.write_text(body)
    return script


class TestDefaultTempDir:
    def test_existing_default_is_kept(self, tmp_path, monkeypatch):
        """A live cached default is returned as-is: no re-resolution, no churn."""
        keep = tmp_path / "live-temp"
        keep.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(keep))
        assert _default_temp_dir() == str(keep)
        assert tempfile.gettempdir() == str(keep)

    def test_vanished_default_is_replaced_by_next_live_candidate(
        self, vanished_tempdir, live_tempdir
    ):
        resolved = Path(_default_temp_dir())
        assert resolved == live_tempdir
        # The vanished directory is never recreated: under the managed scratch
        # root that would be a directory with no owner record, which the sweep
        # never deletes, so a skipped run would become a permanent leak.
        assert not vanished_tempdir.exists()


class TestChildEnv:
    def test_present_temp_keys_point_at_the_live_dir(self, vanished_tempdir, live_tempdir):
        env = _clean_cron_env()
        for key in _TEMP_KEYS:
            assert key in env
            assert Path(env[key]) == live_tempdir

    def test_absent_temp_keys_stay_absent(self, monkeypatch):
        for key in _TEMP_KEYS:
            monkeypatch.delenv(key, raising=False)
        env = _clean_cron_env()
        assert not any(key in env for key in _TEMP_KEYS)


class TestRunScriptSandboxed:
    def test_ungranted_run_survives_vanished_tempdir(
        self, tmp_path, monkeypatch, vanished_tempdir, live_tempdir
    ):
        """Launcher and secret file are born in a directory that exists.

        Both were created with ``dir=None`` -- the cached, vanished default.
        The run must complete, and the launcher must not have been placed
        under the reclaimed directory (which must stay gone).
        """
        seen: dict[str, str] = {}

        def recording_wrap(argv, **kwargs):
            seen["launcher"] = argv[-1]
            return (list(argv), None)

        monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", recording_wrap)
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(str(script) + ":run", "job1", timeout=120)
        assert result["status"] == "ok", result
        launcher = Path(seen["launcher"])
        assert launcher.name.startswith("kirocrew_cron_")
        assert launcher.parent == live_tempdir
        assert not vanished_tempdir.exists()

    def test_granted_run_survives_vanished_tempdir(
        self, tmp_path, monkeypatch, vanished_tempdir, live_tempdir
    ):
        """The private pinned dir is created with ``dir=None`` too, one step
        earlier than the launcher, so a granted run dies at ``mkdtemp``. It
        must instead be born under an existing directory and stay PRIVATE: the
        launcher lives inside it, not in the shared temp dir the fallback
        resolves to."""
        seen: dict[str, str] = {}

        def recording_wrap(argv, **kwargs):
            seen["launcher"] = argv[-1]
            # A granted run refuses an argv the wrap handed back UNMODIFIED
            # (no sandbox backend), so the stand-in must alter it.
            return ([argv[0], "-X", "utf8", *argv[1:]], None)

        monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", recording_wrap)
        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-script")
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        grant = {"MY_SANDBOX_TOKEN": "slack-sandbox"}
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(spec, "", job_id="job1", grant=grant)
            result = run_script_sandboxed(
                spec, "job1", timeout=120, secret_env=grant, secret_env_pin=pin
            )
        assert result["status"] == "ok", result
        launcher = Path(seen["launcher"])
        assert launcher.parent.name.startswith("kirocrew_cron_pin_")
        assert launcher.parent.parent == live_tempdir
        assert not vanished_tempdir.exists()
