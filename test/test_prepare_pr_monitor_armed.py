"""Tests for the prepare-pr monitor_armed.py loop-arming check.

``monitor_start`` is a stateless session directive: the MCP tool returns "Monitor
loop requested" and the loop is armed later, when the turn's tool result is
consumed. Every drop on that path is silent to the model, so prepare-pr Phase 3
verifies arming against the loop store instead of the reply text -- and these
tests pin the two properties that verification depends on:

- the exit contract (0 armed / 20 not armed / 2 store unreadable), because the
  skill branches on it and a wrong code either strands a PR with nothing polling
  it or declares a loop that does not exist;
- the output allowlist: a persisted loop carries its full re-injected instruction
  (``message``) and ``stop_sentinel_path``, and this script's output lands in a
  chat transcript, so neither may appear in what it prints.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MONITOR_ARMED = str(
    REPO_ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "prepare-pr"
    / "scripts"
    / "monitor_armed.py"
)

#: Stand-in for the sensitive halves of a persisted loop: the instruction text is
#: LLM/user-authored and the sentinel is a local path.
CANARY = "LEAK-CANARY-b3f1"


def _store(**overrides: object) -> dict:
    """A loop store holding one active loop for PR 6712 and one stopped loop."""
    active = {
        "id": "loop-1",
        "slot_key": "chat-1",
        "active": True,
        "cycle_count": 3,
        "max_cycles": 80,
        "idle_secs": 300,
        "next_due_ts": 123.0,
        "message": "Re-poll PR #6712 with pr_status.py " + CANARY,
        "stop_sentinel_path": "/tmp/" + CANARY + "/STOP",
    }
    active.update(overrides)
    stopped = {
        "id": "loop-2",
        "slot_key": "chat-2",
        "active": False,
        "cycle_count": 9,
        "message": "Re-poll PR #999",
    }
    return {"version": 1, "loops": [active, stopped]}


def _write_store(home: Path, payload: object) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "autonudge.json").write_text(json.dumps(payload), encoding="utf-8")


def _run(home: Path, *args: str) -> tuple[int, str]:
    """Run monitor_armed.py against *home*; return (rc, stdout + stderr)."""
    env = dict(os.environ, KIROCREW_HOME=str(home))
    proc = subprocess.run(
        [sys.executable, MONITOR_ARMED, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return proc.returncode, proc.stdout + proc.stderr


class TestTheExitContract:
    def test_an_active_loop_naming_the_pr_is_armed(self, tmp_path: Path) -> None:
        _write_store(tmp_path, _store())
        rc, out = _run(tmp_path, "--pr", "6712")
        assert rc == 0, out
        assert "ARMED" in out

    def test_a_shorter_number_does_not_match_a_longer_one(self, tmp_path: Path) -> None:
        # Without word-boundary anchoring, --pr 671 would match "PR #6712" and
        # report a loop that is driving a different PR.
        _write_store(tmp_path, _store())
        rc, _ = _run(tmp_path, "--pr", "671")
        assert rc == 20

    def test_a_stopped_loop_is_not_armed(self, tmp_path: Path) -> None:
        # active=False is how the service records a loop it has torn down; a
        # stopped loop polls nothing.
        _write_store(tmp_path, _store())
        rc, _ = _run(tmp_path, "--pr", "999")
        assert rc == 20

    def test_no_active_loop_at_all_is_not_armed(self, tmp_path: Path) -> None:
        _write_store(tmp_path, {"version": 1, "loops": []})
        rc, _ = _run(tmp_path, "--pr", "6712")
        assert rc == 20

    def test_an_unfiltered_check_accepts_any_active_loop(self, tmp_path: Path) -> None:
        _write_store(tmp_path, _store())
        rc, _ = _run(tmp_path)
        assert rc == 0

    def test_a_missing_store_is_an_environment_error(self, tmp_path: Path) -> None:
        # Nothing has ever armed a loop on this host: exit 2, which the skill
        # routes to the same wait-loop fallback as 20 rather than to "armed".
        rc, _ = _run(tmp_path, "--pr", "6712")
        assert rc == 2

    def test_a_corrupt_store_is_an_environment_error(self, tmp_path: Path) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "autonudge.json").write_text("{not json", encoding="utf-8")
        rc, _ = _run(tmp_path, "--pr", "6712")
        assert rc == 2

    def test_a_bare_list_store_is_still_read(self, tmp_path: Path) -> None:
        _write_store(tmp_path, _store()["loops"])
        rc, _ = _run(tmp_path, "--pr", "6712")
        assert rc == 0


class TestTheOutputCarriesNoLoopPayload:
    def test_the_text_report_omits_the_message_and_sentinel(self, tmp_path: Path) -> None:
        _write_store(tmp_path, _store())
        rc, out = _run(tmp_path, "--pr", "6712")
        assert rc == 0
        assert CANARY not in out
        assert "cycle_count=3" in out  # the status fields the caller does need

    def test_the_json_report_is_a_projection_not_the_stored_loop(self, tmp_path: Path) -> None:
        _write_store(tmp_path, _store())
        rc, out = _run(tmp_path, "--pr", "6712", "--json")
        assert rc == 0
        assert CANARY not in out
        payload = json.loads(out)
        assert payload["armed"] is True
        emitted = set(payload["loops"][0])
        assert "message" not in emitted
        assert "stop_sentinel_path" not in emitted
        assert {"id", "slot_key", "cycle_count"} <= emitted

    def test_an_unmatched_check_reports_only_a_count(self, tmp_path: Path) -> None:
        _write_store(tmp_path, _store())
        rc, out = _run(tmp_path, "--pr", "4242", "--json")
        assert rc == 20
        assert CANARY not in out
        assert json.loads(out) == {"armed": False, "active_loops": 1}
