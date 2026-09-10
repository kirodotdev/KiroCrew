"""CLI move verbs for session and task-run migration.

Tasks 3.8 and 4.8. Drives the same code path argparse would, with the stores
faked, so no real runs.json / dashboard is touched.

`taskrun move` is fully functional from the CLI: runs.json is on disk, so the
plan is real. `session move` deliberately REFUSES from the CLI — a session
bundle must be snapshotted from the live slot (Layer A flush consistency plus
the Layer B join), which the CLI cannot do safely against a running session.
The refusal is the feature: a precise reason beats a racy snapshot.

Side-effect discipline: tmp_path runs file, captured stdout/stderr, no network.
"""

from __future__ import annotations

import argparse
import json

import pytest


def _run_record(task_id="TASK_abc", status="paused"):
    """A record in the shape runs.json actually stores."""
    return {
        "task_id": task_id,
        "name": "nightly build",
        "spec_path": "/repo/spec.md",
        "spec_content": "# plan",
        "status": status,
        "replan_count": 0,
        "repo_root": "/repo",
        "branch_name": "feat/x",
        "worktree_path": "/wt/x",
        "work_dir": "/wt/x",
        "started_at": 111.0,
        "error": "",
        "commit_hashes": ["abc1234"],
        "git_enabled": True,
        "source": "spec",
        "task_details": [
            {
                "index": 0,
                "title": "alpha",
                "status": "passed",
                "requires_approval": False,
                "attempts": 1,
            },
            {
                "index": 1,
                "title": "beta",
                "status": "pending",
                "requires_approval": True,
                "attempts": 0,
            },
        ],
    }


def _runs_file(tmp_path, *records):
    path = tmp_path / "runs.json"
    path.write_text(json.dumps(list(records)), encoding="utf-8")
    return path


# ------------------------------------------------------- taskrun move (4.8)


def test_taskrun_move_prints_a_real_plan_from_runs_json(tmp_path, capsys):
    import kiro_crew.cli_commands as cc

    runs = _runs_file(tmp_path, _run_record())
    args = argparse.Namespace(
        command="taskrun",
        taskrun_action="move",
        task_id="TASK_abc",
        to_crew="remote-ec2",
        runs_file=str(runs),
    )
    cc._taskrun_dispatch(args)
    out = capsys.readouterr().out
    assert "TASK_abc" in out
    assert "remote-ec2" in out
    assert "handoff_id:" in out
    # the run's repo is a NAMED requirement, never a shipped path
    assert "git_repo: /repo" in out


def test_taskrun_move_plan_sanitizes_requirement_identities(tmp_path, capsys):
    """The shared renderer is the terminal boundary, so every verb is covered.

    The cron verb prints its own inline block and was fixed first; this verb
    renders through ``move_plan.render_plan``, which kept printing identities raw.
    That asymmetry is why the sanitizer belongs at the shared renderer rather
    than at each call site. A run's ``repo_root`` becomes a requirement identity,
    so it is the operator-authored text that reaches the terminal here.
    """
    import kiro_crew.cli_commands as cc

    rec = _run_record()
    # A credential AND an OSC title-set sequence inside the identity text.
    rec["repo_root"] = "/repo/AKIAIOSFODNN7EXAMPLE\x1b]0;pwned\x07"
    rec["worktree_path"] = rec["repo_root"]
    rec["work_dir"] = rec["repo_root"]
    runs = _runs_file(tmp_path, rec)
    args = argparse.Namespace(
        command="taskrun",
        taskrun_action="move",
        task_id="TASK_abc",
        to_crew="remote-ec2",
        runs_file=str(runs),
    )
    cc._taskrun_dispatch(args)
    out = capsys.readouterr().out
    assert "AKIAIOSFODNN7EXAMPLE" not in out, "credential reached the terminal"
    assert "\x1b" not in out and "\x07" not in out, "terminal control sequence reached the terminal"


def test_taskrun_move_refuses_a_runs_registry_that_is_not_a_list(tmp_path, capsys):
    """`null` is valid JSON, so the decode guard does not catch it.

    Iterating a scalar raises TypeError past that guard, which reaches the user as
    a traceback instead of a sentence naming the wrong file.
    """
    import kiro_crew.cli_commands as cc

    runs = tmp_path / "runs.json"
    runs.write_text("null", encoding="utf-8")
    args = argparse.Namespace(
        command="taskrun",
        taskrun_action="move",
        task_id="TASK_abc",
        to_crew="remote-ec2",
        runs_file=str(runs),
    )
    with pytest.raises(SystemExit) as exc:
        cc._taskrun_dispatch(args)
    assert exc.value.code == 1
    assert "not a list of run records" in capsys.readouterr().err


def test_taskrun_move_reports_the_state_runs_json_cannot_supply(tmp_path, capsys):
    import kiro_crew.cli_commands as cc

    runs = _runs_file(tmp_path, _run_record())
    args = argparse.Namespace(
        command="taskrun",
        taskrun_action="move",
        task_id="TASK_abc",
        to_crew="dst",
        runs_file=str(runs),
    )
    cc._taskrun_dispatch(args)
    out = capsys.readouterr().out
    # WorkingMemory and current_task are not persisted -- say so
    assert "memory" in out and "current_task" in out


def test_taskrun_move_unknown_run_exits_with_a_clear_error(tmp_path, capsys):
    import kiro_crew.cli_commands as cc

    runs = _runs_file(tmp_path, _run_record())
    args = argparse.Namespace(
        command="taskrun",
        taskrun_action="move",
        task_id="TASK_nope",
        to_crew="dst",
        runs_file=str(runs),
    )
    with pytest.raises(SystemExit):
        cc._taskrun_dispatch(args)
    assert "TASK_nope" in capsys.readouterr().err


def test_taskrun_move_refuses_a_run_with_a_task_mid_execution(tmp_path, capsys):
    import kiro_crew.cli_commands as cc

    rec = _run_record()
    rec["task_details"][1]["status"] = "in_progress"
    runs = _runs_file(tmp_path, rec)
    args = argparse.Namespace(
        command="taskrun",
        taskrun_action="move",
        task_id="TASK_abc",
        to_crew="dst",
        runs_file=str(runs),
    )
    with pytest.raises(SystemExit):
        cc._taskrun_dispatch(args)
    assert "mid-execution" in capsys.readouterr().err


def test_taskrun_move_missing_runs_file_exits(tmp_path, capsys):
    import kiro_crew.cli_commands as cc

    args = argparse.Namespace(
        command="taskrun",
        taskrun_action="move",
        task_id="TASK_abc",
        to_crew="dst",
        runs_file=str(tmp_path / "absent.json"),
    )
    with pytest.raises(SystemExit):
        cc._taskrun_dispatch(args)
    assert "runs" in capsys.readouterr().err.lower()


# ------------------------------------------------------- session move (3.8)


def test_session_move_refuses_from_the_cli_and_says_why(capsys):
    import kiro_crew.cli_commands as cc

    args = argparse.Namespace(
        command="session", session_action="move", session_id="chat-3", to_crew="remote-ec2"
    )
    with pytest.raises(SystemExit):
        cc._session_dispatch(args)
    err = capsys.readouterr().err
    # names the reason, not just "unsupported"
    assert "layer b" in err.lower() or "live" in err.lower()
    assert "chat-3" in err


def test_session_move_points_at_the_surface_that_can_do_it(capsys):
    import kiro_crew.cli_commands as cc

    args = argparse.Namespace(
        command="session", session_action="move", session_id="chat-3", to_crew="dst"
    )
    with pytest.raises(SystemExit):
        cc._session_dispatch(args)
    err = capsys.readouterr().err
    assert "dashboard" in err.lower() or "gateway" in err.lower()
