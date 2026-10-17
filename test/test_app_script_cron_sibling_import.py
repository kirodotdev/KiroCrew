"""An app's cron runs its own bundle code under the cron sandbox.

An app ships cron code inside its bundle, ``<config_dir>/apps/<app>/``, and that code
imports modules beside it -- a sibling ``.py``, a ``lib/`` package. The cron child must be
able to read the bundle it runs from, whether the job is a script cron naming the bundle's
``job.py`` or a command cron running ``python <bundle>/run.py``. A spawn that masks the
apps tree (even one that re-exposes each app's ``data/``) hides the code and every module
it imports, and every such cron then fails before its first line.

Two cases per exec path. The first runs on every host that runs that kind of cron (a
host with no POSIX shell refuses command crons outright, so the command cases stand
aside there): it replaces ``wrap_argv`` with a
passthrough that records what the call site asked the sandbox to hide, runs the real
child, and requires that no hidden directory covers the bundle without a window
re-exposing it. The second runs where this host can carry a caller's masks for real
(``credential_mask_applies``) and spawns through the real sandbox with nothing patched.

Must be runnable with ``--noconftest`` (no hypothesis dependency).
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import cron_script, sandbox
from kiro_crew.cron_script import run_command_sandboxed, run_script_sandboxed

APP = "sibling-app"

_IMPORTS = (
    "import helper\n"
    "from lib.values import ANSWER\n"
    "def _check():\n"
    "    if helper.VALUE != 7 or ANSWER != 42:\n"
    "        raise RuntimeError('bundle modules imported the wrong values')\n"
)
SCRIPT = _IMPORTS + "def run(ctx):\n    _check()\n"
COMMAND_SCRIPT = _IMPORTS + "_check()\nprint('sibling-import-ok')\n"

_NO_BACKEND = pytest.mark.skipif(
    not sandbox.credential_mask_applies("cc"),
    reason="this host has no OS sandbox backend that carries a caller's masks",
)


@pytest.fixture(autouse=True)
def _session_key(monkeypatch):
    """Cron spawn bookkeeping wants a nameable caller; stated without the conftest."""
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:sibling-import-test")


@pytest.fixture()
def bundle(tmp_path, monkeypatch) -> Path:
    """A crew home holding one installed app whose cron code imports its own modules."""
    home = tmp_path / "crew-home"
    (home / "crons").mkdir(parents=True)
    app = home / "apps" / APP
    (app / "lib").mkdir(parents=True)
    (app / "data").mkdir()
    (app / ".app_secret").write_text("not-a-real-secret\n")
    (app / "helper.py").write_text("VALUE = 7\n")
    (app / "lib" / "__init__.py").write_text("")
    (app / "lib" / "values.py").write_text("ANSWER = 42\n")
    (app / "job.py").write_text(SCRIPT)
    (app / "run.py").write_text(COMMAND_SCRIPT)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    # The fresh child interpreter imports ``kiro_crew``; a source checkout is not on its
    # path unless it is put there.
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", src_dir + (os.pathsep + existing if existing else ""))
    return app


def _command(bundle: Path) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(bundle / 'run.py'))}"


def _covers(parent: str, child: str) -> bool:
    parent = os.path.normpath(parent)
    child = os.path.normpath(child)
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _record_spawns(monkeypatch) -> list[tuple[list[str], dict[str, Any]]]:
    """Replace ``wrap_argv`` with a passthrough that records every spawn it is asked for.

    The call site may gate its masks on whether the spawn carries them; the recorder
    answers as a host that does, so the requested masks are the ones a real sandbox
    would apply rather than the empty set a backend-less host is handed.
    """
    seen: list[tuple[list[str], dict[str, Any]]] = []

    def _passthrough(argv, **kwargs):
        seen.append((list(argv), kwargs))
        return list(argv), None

    monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", _passthrough)
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: True)
    monkeypatch.setattr(
        "kiro_crew.cron_script.credential_mask_applies", lambda mode: True, raising=False
    )
    return seen


def _assert_bundle_readable(kwargs: dict[str, Any], bundle: Path) -> None:
    """No requested mask may cover the bundle unless a window re-exposes the bundle itself.

    A window over ``data/`` alone leaves the code and its modules hidden, which is the
    regression this pins.
    """
    hidden = [str(p) for p in kwargs.get("extra_hidden_dirs", ())]
    windows = [str(p) for p in kwargs.get("extra_private_dirs", ())]
    for mask in hidden:
        if _covers(mask, str(bundle)):
            assert any(_covers(w, str(bundle)) for w in windows), (
                f"{mask} hides the app bundle {bundle}, so the cron's code and the "
                f"modules it imports are unreadable in the child; windows: {windows}"
            )


class TestAScriptCronNamingTheBundle:
    def test_the_call_site_leaves_the_bundle_readable(self, bundle, monkeypatch):
        seen = _record_spawns(monkeypatch)

        result = run_script_sandboxed(f"{bundle / 'job.py'}:run", "job-sibling", timeout=60)

        assert len(seen) == 1, "the script cron must spawn through wrap_argv exactly once"
        assert result["status"] == "ok", result
        _assert_bundle_readable(seen[0][1], bundle)

    @_NO_BACKEND
    def test_the_real_sandbox_runs_it(self, bundle):
        result = run_script_sandboxed(f"{bundle / 'job.py'}:run", "job-sibling", timeout=120)

        assert result["status"] == "ok", result


class TestACommandCronRunningBundleCode:
    def test_the_call_site_leaves_the_bundle_readable(self, bundle, monkeypatch):
        seen = _record_spawns(monkeypatch)
        # A host with no POSIX shell refuses every command cron before it spawns
        # (``_no_command_shell_message``; Windows by design), so there is no mask to
        # check. The probe runs AFTER the recorder is in place: unpatched, it spawns
        # through the real ``wrap_argv``, which raises on a POSIX host with no sandbox
        # backend and would read that host as shell-less -- the very host this case
        # is written for. A cached verdict from an earlier probe is cleared first.
        monkeypatch.setattr(cron_script, "_POSIX_STRICT_CACHE", {})
        if cron_script._resolve_command_shell() is None:
            pytest.skip("this host refuses command crons: no POSIX shell to run them")
        seen.clear()

        result = run_command_sandboxed(_command(bundle), timeout=60, job_id="job-sibling")

        # The shell probe may spawn through ``wrap_argv`` too; the command's own spawn is
        # the one whose argv carries the command.
        spawns = [kw for argv, kw in seen if any(str(bundle / "run.py") in a for a in argv)]
        assert len(spawns) == 1, seen
        assert result["status"] == "ok", result
        assert "sibling-import-ok" in result["output"]
        _assert_bundle_readable(spawns[0], bundle)

    @_NO_BACKEND
    def test_the_real_sandbox_runs_it(self, bundle):
        # Nothing is patched here, so the real probe answers: a host whose shell fails
        # it refuses command crons, and there is no run to check.
        if cron_script._resolve_command_shell() is None:
            pytest.skip("this host refuses command crons: no POSIX shell to run them")
        result = run_command_sandboxed(_command(bundle), timeout=120, job_id="job-sibling")

        assert result["status"] == "ok", result
        assert "sibling-import-ok" in result["output"]
