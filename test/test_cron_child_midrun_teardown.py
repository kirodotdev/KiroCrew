"""A cron script child must not report success once it cannot persist.

1. A data home that dies WHILE the script body runs must not end as ``ok``: the
   launcher probes again after the body, before any non-error envelope.
2. A non-zero child exit is a failure even when the script printed a
   status-shaped line of its own.
3. The child's own tool-call audit writes synchronously, so an ``ENOSYS`` there
   reaches ``refuse_unaudited_on_dead_fs`` instead of the background writer.

Builtin fixtures only, so the module runs under ``--noconftest`` too.
"""

from __future__ import annotations

import errno
import os
import types
from pathlib import Path

import pytest

from kiro_crew import cron_script
from kiro_crew.cron_script import CHILD_PERSISTENCE_PREFIX, ScriptContext, run_script_sandboxed
from kiro_crew.sandbox import CRON_SCRIPT_CHILD_ENV, UnauditedSpawnRefused


@pytest.fixture
def cron_home(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    crons = home / "crons"
    crons.mkdir()
    return crons


@pytest.fixture
def spawns_real_child(monkeypatch, tmp_path):
    """Run the REAL launcher with the OS wrap bypassed (no backend on CI)."""
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", src_dir + (os.pathsep + existing if existing else ""))
    monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None))
    monkeypatch.chdir(tmp_path)


def _child_home(monkeypatch, home: Path) -> None:
    """Point only the CHILD's ``KIROCREW_HOME`` at *home*."""
    real = cron_script._clean_cron_env

    def _with_home() -> dict[str, str]:
        env = real()
        env["KIROCREW_HOME"] = str(home)
        return env

    monkeypatch.setattr(cron_script, "_clean_cron_env", _with_home)


def _write(crons_dir: Path, code: str) -> str:
    script = crons_dir / "midrun.py"
    script.write_text(code, newline="\n", encoding="utf-8")
    return str(script) + ":run"


#: The prefix without its leading emoji: a Windows child's stderr is not UTF-8,
#: so the emoji arrives as ``\u274c`` there while the words survive unchanged.
_PERSISTENCE_TEXT = CHILD_PERSISTENCE_PREFIX.split(" ", 1)[1]

# The body replaces its own data home with a plain file: the preamble probe has
# already passed, and every later probe of that path fails on every platform.
_TEARDOWN = (
    "import os, shutil\n"
    "def _teardown():\n"
    "    home = os.environ['KIROCREW_HOME']\n"
    "    shutil.rmtree(home)\n"
    "    with open(home, 'w') as f:\n"
    "        f.write('gone')\n"
)


class TestMidRunTeardown:
    @pytest.mark.parametrize(
        "body",
        [
            "def run(ctx):\n    _teardown()\n",
            "from kiro_crew.cron_script import Skip\n"
            "def run(ctx):\n    _teardown()\n    raise Skip()\n",
            "from kiro_crew.cron_script import Done\n"
            "def run(ctx):\n    _teardown()\n    raise Done('banked')\n",
            "from kiro_crew.cron_script import Report\n"
            "def run(ctx):\n    _teardown()\n    raise Report('banked')\n",
        ],
        ids=["ok", "skip", "done", "report"],
    )
    def test_a_home_lost_during_the_run_is_an_error(
        self, body, cron_home, spawns_real_child, monkeypatch, tmp_path
    ):
        child_home = tmp_path / "child-home"
        child_home.mkdir()
        _child_home(monkeypatch, child_home)
        result = run_script_sandboxed(_write(cron_home, _TEARDOWN + body), "job-id", timeout=120)

        assert result["status"] == "error", f"mid-run teardown recorded as success: {result!r}"
        assert _PERSISTENCE_TEXT in result["error"], result

    def test_a_healthy_run_still_reports_ok(self, cron_home, spawns_real_child):
        result = run_script_sandboxed(
            _write(cron_home, "def run(ctx):\n    pass\n"), "job-id", timeout=120
        )
        assert result == {"status": "ok"}


class TestNonZeroExitWithStdout:
    def test_status_line_on_a_non_zero_exit_is_an_error(self, cron_home, spawns_real_child):
        script = _write(
            cron_home,
            "import json, sys\n"
            "def run(ctx):\n"
            "    print(json.dumps({'status': 'ok'}), flush=True)\n"
            "    sys.stderr.write('boom from the script\\n')\n"
            "    sys.exit(3)\n",
        )
        result = run_script_sandboxed(script, "job-id", timeout=120)

        assert result["status"] == "error", f"non-zero exit masked by stdout: {result!r}"
        assert "boom from the script" in result["error"]

    def test_non_zero_exit_with_no_stderr_names_the_code(self, cron_home, spawns_real_child):
        script = _write(
            cron_home,
            "import sys\n"
            "def run(ctx):\n"
            "    print('partial work', flush=True)\n"
            "    sys.exit(5)\n",
        )
        result = run_script_sandboxed(script, "job-id", timeout=120)
        assert result == {"status": "error", "error": "exit 5"}


class _FailingSel:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls: list[dict] = []

    def log_tool_invocation(self, **kwargs):
        self.calls.append(kwargs)
        raise self.exc


def _ctx() -> ScriptContext:
    return ScriptContext(job=types.SimpleNamespace(id="job-id", message=""))


class TestToolCallAudit:
    def test_audit_write_is_synchronous(self, monkeypatch):
        fake = _FailingSel(OSError(errno.EIO, "disk"))
        monkeypatch.setattr(cron_script, "sel", lambda: fake)
        monkeypatch.delenv(CRON_SCRIPT_CHILD_ENV, raising=False)
        _ctx()._audit_tool_call("srv", "tool", "ok")
        assert fake.calls and fake.calls[0].get("critical") is True

    def test_enosys_in_a_cron_child_refuses(self, monkeypatch):
        fake = _FailingSel(OSError(errno.ENOSYS, "Function not implemented"))
        monkeypatch.setattr(cron_script, "sel", lambda: fake)
        monkeypatch.setenv(CRON_SCRIPT_CHILD_ENV, "1")
        with pytest.raises(UnauditedSpawnRefused, match="ENOSYS"):
            _ctx()._audit_tool_call("srv", "tool", "ok")

    def test_enosys_outside_a_cron_child_degrades(self, monkeypatch):
        fake = _FailingSel(OSError(errno.ENOSYS, "Function not implemented"))
        monkeypatch.setattr(cron_script, "sel", lambda: fake)
        monkeypatch.delenv(CRON_SCRIPT_CHILD_ENV, raising=False)
        _ctx()._audit_tool_call("srv", "tool", "ok")
        # The write was attempted for this job's call; only its failure is tolerated.
        assert [(c["session_key"], c["tool_name"]) for c in fake.calls] == [
            ("cron:job-id", "srv/tool")
        ]

    def test_other_errors_in_a_cron_child_degrade(self, monkeypatch):
        fake = _FailingSel(OSError(errno.EIO, "disk"))
        monkeypatch.setattr(cron_script, "sel", lambda: fake)
        monkeypatch.setenv(CRON_SCRIPT_CHILD_ENV, "1")
        _ctx()._audit_tool_call("srv", "tool", "ok")
        assert [(c["session_key"], c["tool_name"]) for c in fake.calls] == [
            ("cron:job-id", "srv/tool")
        ]
