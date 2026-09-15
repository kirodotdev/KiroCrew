"""Real pytest teardown, not a hand-written imitation of marker ownership."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from cli_test_helpers import cli_sandbox_environment  # noqa: F401

# This probe observes the actual fixtures of the selected modules. It never
# restores markers itself: a missed import or wrong teardown must remain visible.
PYTEST_ENV_PROBE = r"""
import json
import os
import sys
import pytest

keys = ("KIROCREW_SANDBOX_ACTIVE", "KIROCREW_SANDBOX_LEVEL")
def snapshot():
    return {key: os.environ.get(key) for key in keys}

before = snapshot()
records = []
class Audit:
    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        entry = snapshot()
        result = yield
        after = snapshot()
        records.append({"node": item.nodeid, "before": entry, "after": after,
                        "equal": entry == after, "entry_matches_process": entry == before,
                        "fixture": "cli_sandbox_environment" in item.fixturenames})
        return result

code = pytest.main(["-q", "-n0", "-p", "no:cacheprovider",
                    "--basetemp", sys.argv[1], *sys.argv[2:]], plugins=[Audit()])
after = snapshot()
print("CLI_ENV_EVIDENCE=" + json.dumps({"pytest_exit": int(code), "before": before,
      "after": after, "equal": before == after, "items": records}), flush=True)
raise SystemExit(int(code) or int(before != after or any(
    not row["equal"] or not row["entry_matches_process"] for row in records)))
"""


PRODUCERS = [
    "test/test_cli.py::TestPortEnvValidatedAtEntry::test_in_range_port_is_accepted",
    "test/test_cli_help.py::TestGroupingCoversEveryCommand::test_offered_and_grouped_sets_match",
    "test/test_cli_lazy_imports.py::test_boot_platform_runs_before_mcp_core_dispatch",
    "test/test_cli.py::TestSandboxActiveMarkerCleared::test_main_clears_inherited_sandbox_active_marker",
    "test/test_cli_environment.py::test_dispatch_after_shared_monkeypatch_undo",
]


def test_dispatch_after_shared_monkeypatch_undo(monkeypatch):
    """Early shared undo must not release the CLI fixture's independent teardown."""
    from kiro_crew.cli import main

    monkeypatch.setenv("CLI_ENV_TEST_SENTINEL", "temporary")
    monkeypatch.undo()
    observed = []

    def capture(_args):
        observed.append(
            (os.environ.get("KIROCREW_SANDBOX_ACTIVE"), os.environ.get("KIROCREW_SANDBOX_LEVEL"))
        )

    with (
        patch.object(sys, "argv", ["kirocrew", "cron", "list"]),
        patch("kiro_crew.cli_commands._cron", capture),
    ):
        main()
    assert observed == [(None, None)]


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
@pytest.mark.parametrize(
    "entry",
    [None, (None, None), ("1", "standard"), ("1", None), (None, "strict"), ("", "")],
    ids=["inherited", "absent", "present", "active-only", "level-only", "empty"],
)
def test_real_cli_producers_restore_entry_environment(tmp_path, entry, reverse):
    """Every real producer teardown is followed by an observing consumer hook.

    The inherited arm keeps the subprocess's ambient markers untouched. Other
    arms simulate entry states only in this dedicated test child; no backend is
    launched and the parent process environment is never changed.
    """
    env = os.environ.copy()
    if entry is not None:
        for key, value in zip(("KIROCREW_SANDBOX_ACTIVE", "KIROCREW_SANDBOX_LEVEL"), entry):
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
    env["KIROCREW_HOME"] = str(tmp_path / "home")
    env["TMPDIR"] = str(tmp_path / "tmp")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPYCACHEPREFIX"] = str(tmp_path / "pycache")
    (tmp_path / "tmp").mkdir()
    nodes = list(reversed(PRODUCERS)) if reverse else PRODUCERS
    result = subprocess.run(
        [sys.executable, "-B", "-c", PYTEST_ENV_PROBE, str(tmp_path / "pytest"), *nodes],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout.split("CLI_ENV_EVIDENCE=", 1)[1])
    assert evidence["pytest_exit"] == 0
    assert evidence["equal"], evidence
    assert len(evidence["items"]) == len(nodes)
    assert [row["node"] for row in evidence["items"]] == nodes
    assert all(row["fixture"] and row["equal"] for row in evidence["items"]), evidence
    # Keep concrete before/after evidence visible in verbose/raw validation logs.
    print("CLI_ENV_MATRIX=" + json.dumps(evidence, sort_keys=True))
