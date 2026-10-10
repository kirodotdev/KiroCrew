"""Script and command crons run with a per-run managed temp dir.

Without one the child inherits the gateway's temp dir -- ``/tmp`` under the
shipped systemd unit, a tmpfs on many distributions -- so a cron's clones,
venvs and temp files are charged to RAM. Each run gets its own
``agent_scratch`` directory, carved out of the masked scratch root as a
private sandbox window, owned by the child's pid so the existing sweep
reclaims it.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_crew.cron_script as cron_script
from kiro_crew import agent_scratch, sandbox
from kiro_crew.cron_script import run_command_sandboxed, run_script_sandboxed

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX spawn plumbing")

TEMP_KEYS = ("TMPDIR", "TMP", "TEMP")


@pytest.fixture(autouse=True)
def _isolated_home(_floor_monkeypatch, tmp_path, named_cron_caller):
    monkeypatch = _floor_monkeypatch
    """A throwaway data home, never the live one, and a passthrough sandbox."""
    home = tmp_path / "crew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr("kiro_crew.cron_script.config_dir", lambda: home)
    monkeypatch.setattr("kiro_crew.agent_scratch.config_dir", lambda: home)
    # The gateway under the shipped systemd unit carries no TMPDIR at all.
    for key in TEMP_KEYS:
        monkeypatch.delenv(key, raising=False)
    src_dir = str(Path(cron_script.__file__).resolve().parents[1])
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", src_dir + (os.pathsep + existing if existing else ""))
    return home


@pytest.fixture
def wrap_calls(monkeypatch, posix_test_shell):
    calls: list[dict] = []

    def _wrap(argv, **kwargs):
        calls.append(kwargs)
        return list(argv), None

    monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", _wrap)
    monkeypatch.setattr("kiro_crew.cron_script._resolve_command_shell", lambda: posix_test_shell)
    return calls


def _scratch_entries() -> list[Path]:
    root = agent_scratch.scratch_root()
    return sorted(root.iterdir()) if root.is_dir() else []


def _assert_run_scratch(seen: str, job_id: str, wrap_calls: list[dict]) -> Path:
    values = seen.split("|")
    assert len(values) == 3 and len(set(values)) == 1, values
    scratch = Path(values[0])
    assert scratch.parent == agent_scratch.scratch_root()
    assert scratch.name.startswith(f"cron-{job_id}-")
    # The scratch root is masked for sandboxed children: the run's own dir
    # must be carved back out, or the child's TMPDIR names a path it cannot see.
    assert wrap_calls[-1]["extra_private_dirs"] == (str(scratch),)
    return scratch


def test_command_cron_gets_its_own_temp_dir_owned_by_the_child(wrap_calls):
    result = run_command_sandboxed(
        'printf %s "$TMPDIR|$TMP|$TEMP"; echo hi > "$TMPDIR/probe"',
        job_id="cmdjob",
    )
    assert result["status"] == "ok", result
    scratch = _assert_run_scratch(result["output"], "cmdjob", wrap_calls)
    assert (scratch / "probe").read_text().strip() == "hi"
    owners = agent_scratch._read_owner_pids(scratch / agent_scratch.OWNER_FILENAME)
    # The child's pid, not the gateway's: a gateway-owned dir is never swept.
    assert owners and os.getpid() not in owners


def test_script_cron_gets_its_own_temp_dir(tmp_path, wrap_calls):
    crons = cron_script.config_dir() / "crons"
    crons.mkdir(parents=True, exist_ok=True)
    script = crons / "scratch_probe.py"
    script.write_text(
        "import os, tempfile\n"
        "from kiro_crew.cron_script import Done\n"
        "def run(ctx):\n"
        "    vals = '|'.join(os.environ.get(k, '') for k in ('TMPDIR', 'TMP', 'TEMP'))\n"
        "    assert tempfile.gettempdir() == os.environ['TMPDIR']\n"
        "    raise Done(vals)\n"
    )
    with patch("pathlib.Path.home", return_value=tmp_path):
        result = run_script_sandboxed(f"{script}:run", "scriptjob", timeout=30)
    assert result["status"] == "done", result
    _assert_run_scratch(result["message"], "scriptjob", wrap_calls)


def test_allocation_failure_degrades_to_inherited_temp(monkeypatch, wrap_calls):
    def _boom(label):
        raise OSError("disk full")

    monkeypatch.setattr(agent_scratch, "allocate_scratch", _boom)
    result = run_command_sandboxed('printf "[%s]" "${TMPDIR-unset}"', job_id="degraded")
    assert result["status"] == "ok", result
    assert result["output"] == "[unset]"
    assert wrap_calls[-1]["extra_private_dirs"] == ()


def test_spawn_failure_removes_the_unused_dir(monkeypatch, wrap_calls):
    def _no_spawn(*a, **k):
        raise OSError("exec failed")

    monkeypatch.setattr(cron_script, "popen_limited", _no_spawn)
    result = run_command_sandboxed("true", job_id="nospawn")
    assert result["status"] == "error"
    # Its marker names the live gateway, so the sweep would keep it forever.
    assert _scratch_entries() == []


@pytest.mark.parametrize("outcome", ["refused", "stale"])
def test_unsafe_owner_record_stops_the_run(monkeypatch, wrap_calls, outcome):
    monkeypatch.setattr(agent_scratch, "record_owner", lambda path, pid: outcome)
    result = run_command_sandboxed("sleep 30; echo ran", job_id="unsafe", timeout=60)
    assert result["status"] == "error"
    assert result["output"] == cron_script._SCRATCH_OWNER_ERROR
    # The sweep never reclaims a gateway-named or linked marker.
    assert _scratch_entries() == []


def test_cancel_racing_the_spawn_removes_the_dir(monkeypatch, wrap_calls):
    monkeypatch.setattr(cron_script, "_finish_spawn", lambda job_id, proc: True)
    result = run_command_sandboxed("sleep 30", job_id="raced", timeout=60)
    assert result["status"] == "cancelled"
    assert _scratch_entries() == []


_NO_BACKEND = pytest.mark.skipif(
    not sandbox.credential_mask_applies("cc"),
    reason="this host has no OS sandbox backend that carries a caller's masks",
)


@_NO_BACKEND
class TestUnderTheRealSandbox:
    """No ``wrap_argv`` stub: the window must open under the cron's real profile.

    The scratch root is masked for every sandboxed child, so a ``TMPDIR`` under
    it is only usable if ``extra_private_dirs`` actually re-exposes the run's
    own directory read-write under ``cc`` (both paths) and ``strict`` (granted
    scripts use the same carve-out).
    """

    def test_command_cron_writes_into_its_temp_dir(self):
        if cron_script._resolve_command_shell() is None:
            pytest.skip("this host refuses command crons: no POSIX shell to run them")
        result = run_command_sandboxed(
            'f=$(mktemp) && echo ok > "$f" && cat "$f" && printf "|%s" "$TMPDIR"',
            job_id="realcmd",
            timeout=120,
        )
        assert result["status"] == "ok", result
        body, scratch = result["output"].strip().split("|")
        assert body.strip() == "ok"
        assert Path(scratch).parent == agent_scratch.scratch_root()

    def test_script_cron_writes_into_its_temp_dir(self, tmp_path):
        crons = cron_script.config_dir() / "crons"
        crons.mkdir(parents=True, exist_ok=True)
        script = crons / "real_scratch_probe.py"
        script.write_text(
            "import os, tempfile\n"
            "from kiro_crew.cron_script import Done\n"
            "def run(ctx):\n"
            "    with tempfile.NamedTemporaryFile('w', delete=False) as fh:\n"
            "        fh.write('ok')\n"
            "    raise Done(os.path.dirname(fh.name))\n"
        )
        result = run_script_sandboxed(f"{script}:run", "realscript", timeout=120)
        assert result["status"] == "done", result
        assert Path(result["message"]).parent == agent_scratch.scratch_root()


def test_command_run_without_a_job_id_still_gets_a_temp_dir(wrap_calls):
    result = run_command_sandboxed('printf %s "$TMPDIR"')
    assert result["status"] == "ok", result
    scratch = Path(result["output"])
    assert scratch.parent == agent_scratch.scratch_root()
    assert scratch.name.startswith("cron-adhoc-")
