"""``kirocrew eval run --suite tasks``: cases, preflight, one run end to end, scoring, summary.

The shipped cases are built for real (``sh`` and ``git``) and judged by their
real hidden checks. No model runs: the Claude Code arm is a fake ``claude`` on
PATH that edits the repo the way an agent would, and the Crew arm's gateway is
replaced, so only the runner's own order (check before the gateway stops) is
exercised. rfc-builtin-eval.md §3-§5, §7, §13.3.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import cli_commands_eval, cli_parser_admin
from kiro_crew.eval import tasks
from kiro_crew.eval.arms import ARMS, ArmOutcome, CrewChatArm
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import default_timeout, wait_until

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the tasks suite is POSIX only")

SUITE = Path(__file__).resolve().parents[1] / "evals" / "tasks"


@pytest.fixture(autouse=True)
def _bounded_subprocess_waits(_floor_monkeypatch):
    """Every subprocess deadline here fails inside the test's own timeout."""
    _floor_monkeypatch.setattr(tasks, "BUILD_TIMEOUT_SECS", default_timeout())
    _floor_monkeypatch.setattr(tasks, "CHECK_TIMEOUT_SECS", default_timeout())
    _floor_monkeypatch.setattr(tasks, "KILL_GRACE_SECS", 0)


S1 = SUITE / "cases" / "simple" / "S1-image-name-cap"


def _case_copy(tmp_path: Path, **over) -> Path:
    dest = tmp_path / "suite" / "cases" / "simple" / "S1-image-name-cap"
    shutil.copytree(S1, dest)
    data = json.loads((dest / "case.json").read_text())
    data.update(over)
    (dest / "case.json").write_text(json.dumps(data))
    # The copied build.sh reaches lib/ by a relative path.
    shutil.copytree(SUITE / "lib", tmp_path / "suite" / "lib")
    return dest


# ---------- cases ----------
def test_every_shipped_case_loads():
    cases = tasks.find_cases(SUITE, "all", [])
    assert {c.id: c.tier for c in cases} == {"S1-image-name-cap": "simple", "M1-ics-feed": "medium"}


def test_every_shipped_case_bites_on_its_base_and_passes_on_its_reference():
    assert tasks.check_cases(tasks.find_cases(SUITE, "all", [])) == []


def test_a_build_commit_starts_no_background_git_maintenance(tmp_path):
    # A detached `git maintenance run --auto` holds objects/maintenance.lock while the
    # build's local clone copies objects/, and removes it mid-copy.
    repo, trace = tmp_path / "repo", tmp_path / "trace.log"
    env = {**tasks._base_env(), "GIT_TRACE": str(trace)}
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    for cmd in (
        ["git", "init", "-q", str(repo)],
        ["git", "-C", str(repo), *ident, "commit", "-q", "--allow-empty", "-m", "base"],
    ):
        subprocess.run(cmd, check=True, env=env, timeout=default_timeout())
    assert "maintenance run" not in trace.read_text()


def test_a_reference_push_starts_no_background_git_maintenance(tmp_path, monkeypatch):
    origin, repo, trace = tmp_path / "origin.git", tmp_path / "repo", tmp_path / "trace.log"
    case = SimpleNamespace(path=tmp_path / "case")
    (case.path / "ref").mkdir(parents=True)
    env = tasks._base_env()
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    for cmd in (
        ["git", "init", "-q", "--bare", f"--initial-branch={tasks.TASK_BRANCH}", str(origin)],
        ["git", "clone", "-q", str(origin), str(repo)],
        ["git", "-C", str(repo), *ident, "commit", "-q", "--allow-empty", "-m", "base"],
    ):
        subprocess.run(cmd, cwd=tmp_path, check=True, env=env, timeout=default_timeout())
    env.update(
        GIT_TRACE=str(trace),
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    monkeypatch.setattr(tasks, "_base_env", lambda: env)
    tasks.apply_reference(case, repo, origin)
    assert "maintenance run" not in trace.read_text(encoding="utf-8")


def test_check_names_a_case_whose_base_already_passes(tmp_path):
    case_dir = _case_copy(tmp_path)
    shutil.copy(case_dir / "ref" / "attach_names.py", case_dir / "base" / "attach_names.py")
    problems = tasks.check_cases([tasks.load_case(case_dir)])
    assert problems == ["S1-image-name-cap: the hidden check passes on the untouched base"]


@pytest.mark.parametrize(
    ("over", "words"),
    [
        ({"tier": "medium"}, "sits under simple/"),
        ({"id": "other"}, "does not match"),
        ({"seed": "no placeholder"}, "{repo}"),
        ({"seed": "/review {repo}"}, "may not open with"),
        ({"timeout_minutes": 999}, "exceed"),
        ({"spend_cap": {"credits": 30}}, "missing or bad field"),
        ({"kind": "docs"}, "only kind"),
    ],
)
def test_a_bad_case_json_is_named(tmp_path, over, words):
    with pytest.raises(tasks.CaseError, match=None) as err:
        tasks.load_case(_case_copy(tmp_path, **over))
    assert words in str(err.value)


def test_an_unknown_case_id_is_refused():
    with pytest.raises(tasks.CaseError, match="no such case: nope"):
        tasks.find_cases(SUITE, "all", ["nope"])


def test_case_caps_never_exceed_the_tier():
    case = tasks.find_cases(SUITE, "simple", [])[0]
    assert case.caps() == tasks.Caps(
        timeout_s=1200, credits=30, usd=3
    )  # flake-ok: the tier cap compared as data


# ---------- preflight ----------
def _pw(name, uid, home, shell="/bin/bash"):
    return SimpleNamespace(pw_name=name, pw_uid=uid, pw_dir=str(home), pw_shell=shell)


def _fake_users(monkeypatch, me, others):
    import pwd

    monkeypatch.setattr(pwd, "getpwuid", lambda uid: me)
    monkeypatch.setattr(pwd, "getpwall", lambda: [me, *others])


def test_preflight_refuses_credential_dirs_and_a_readable_person_home(monkeypatch, tmp_path):
    mine, theirs, service = tmp_path / "eval", tmp_path / "alice", tmp_path / "svc"
    for d in (mine / ".ssh", theirs, service, mine / ".kiro" / "crew" / "diag"):
        d.mkdir(parents=True)
    (mine / ".netrc").write_text("")  # a credential file the sandbox hides, not only a dir
    me = _pw("eval", 4000, mine)
    _fake_users(
        monkeypatch,
        me,
        [
            _pw("alice", 4242, theirs),
            _pw("svc", 4343, service, "/usr/sbin/nologin"),
            _pw("mac", 501, theirs),  # macOS numbers login users from 501
        ],
    )
    reasons = tasks.preflight([ARMS["codex"]], {})
    assert reasons == [
        f"{mine}/.ssh exists: run the eval as a dedicated user with no credentials",
        f"{mine}/.netrc exists: run the eval as a dedicated user with no credentials",
        f"alice's home {theirs} is readable by the eval user",
        f"mac's home {theirs} is readable by the eval user",
    ]


def test_preflight_skips_a_true_shell_service_account_but_not_a_real_shell(monkeypatch, tmp_path):
    mine, home = tmp_path / "eval", tmp_path / "svc"
    mine.mkdir()
    home.mkdir()
    _fake_users(
        monkeypatch,
        _pw("eval", 4000, mine),
        [
            _pw("snitch", 43318, home, "/bin/true"),
            _pw("usr-true", 43319, home, "/usr/bin/true"),
            _pw("untrue", 43320, home, "/usr/bin/untrue"),  # basename, not a substring
            _pw("carol", 43321, home, "/bin/zsh"),
        ],
    )
    assert tasks.preflight([], {}) == [
        f"untrue's home {home} is readable by the eval user",
        f"carol's home {home} is readable by the eval user",
    ]


def test_preflight_passes_a_clean_eval_user(monkeypatch, tmp_path):
    (tmp_path / "eval").mkdir()
    _fake_users(
        monkeypatch,
        _pw("eval", 4000, tmp_path / "eval"),
        [_pw("bob", 4242, tmp_path / "gone")],
    )
    monkeypatch.setattr(ARMS["claude-code"], "binary", lambda: "/bin/claude")
    assert tasks.preflight([ARMS["claude-code"]], {"ANTHROPIC_API_KEY": "k"}) == []
    assert tasks.preflight([ARMS["claude-code"]], {})[0].startswith("claude-code: neither")


def test_preflight_refuses_a_crew_arm_without_a_sandbox_backend(monkeypatch, tmp_path):
    from kiro_crew import sandbox

    (tmp_path / "eval").mkdir()
    _fake_users(monkeypatch, _pw("eval", 4000, tmp_path / "eval"), [])
    monkeypatch.setattr(sandbox, "detect_backend", lambda mode="auto": "none")
    reasons = tasks.preflight([ARMS["crew-chat"]], {"KIRO_API_KEY": "k"})
    assert reasons == ["no sandbox backend: a Crew arm needs the strict sandbox tier"]


def test_preflight_refuses_windows(monkeypatch):
    monkeypatch.setattr(tasks.os, "name", "nt")
    assert tasks.preflight([], {}) == [
        "kirocrew eval run --suite tasks runs on Linux and macOS only"
    ]


def test_a_run_env_keeps_only_its_homes_base_keys_and_its_arm_sign_in(tmp_path):
    dirs = tasks.RunDirs(tmp_path / "r")
    source = {
        "PATH": "/bin",
        "KIRO_API_KEY": "kiro",
        "ANTHROPIC_API_KEY": "anthropic",
        "AWS_SECRET_ACCESS_KEY": "leak",
        "HOME": "/nonexistent/operator-home",
        "GH_TOKEN": "leak",
    }
    env = tasks.run_env(dirs, ARMS["crew-chat"], source)
    assert env == {
        "PATH": "/bin",
        "HOME": str(dirs.home),
        "KIROCREW_HOME": str(dirs.crew),
        "KIRO_HOME": str(dirs.kiro),
        "TMPDIR": str(dirs.tmp),
        "KIRO_API_KEY": "kiro",
    }
    assert oct(dirs.home.stat().st_mode & 0o777) == "0o700"


# ---------- scoring ----------
@pytest.mark.parametrize(
    ("outcome", "passed", "wall", "audit", "verdict"),
    [
        (ArmOutcome("void", note="no gateway"), True, 1, "", ("VOID", "no gateway")),
        (ArmOutcome("done"), True, 1, "audit: x", ("VOID", "audit: x")),
        (ArmOutcome("done", asks=1), True, 1, "", ("FAILED", "ask")),
        (ArmOutcome("asked", asks=1), False, 1, "", ("FAILED", "ask")),
        (ArmOutcome("timeout"), True, 1, "", ("FAILED", "timeout")),
        (ArmOutcome("done"), True, 99, "", ("FAILED", "timeout")),
        (ArmOutcome("budget"), True, 1, "", ("FAILED", "spend_cap")),
        # Spend that settled after the arm's last poll: the final cost is over the cap.
        (ArmOutcome("done", cost=31.0, cost_unit="credits"), True, 1, "", ("FAILED", "spend_cap")),
        (ArmOutcome("done", cost=3.5, cost_unit="usd"), True, 1, "", ("FAILED", "spend_cap")),
        (ArmOutcome("done", cost=30.0, cost_unit="credits"), True, 1, "", ("PASSED", "")),
        # Cost unknown (chats left open), but what was counted is already over the cap.
        (
            ArmOutcome("done", cost=None, cost_floor=31.0, cost_unit="credits"),
            True,
            1,
            "",
            ("FAILED", "spend_cap"),
        ),
        (ArmOutcome("incomplete"), True, 1, "", ("FAILED", "incomplete")),
        (ArmOutcome("done"), False, 1, "", ("FAILED", "hidden_check")),
        (ArmOutcome("done"), True, 1, "", ("PASSED", "")),
    ],
)
def test_score_follows_the_rfc_rules_in_order(outcome, passed, wall, audit, verdict):
    caps = tasks.Caps(10, 30, 3)
    assert tasks.score(outcome, {"pass": passed}, wall, caps, audit) == verdict


def test_a_slow_check_sample_does_not_hold_the_arm(tmp_path, monkeypatch):
    """The arm exits while a sample is still running: drive_arm returns without it."""
    monkeypatch.setattr(tasks, "SAMPLE_SECS", 0)
    monkeypatch.setattr(tasks, "CHECK_TIMEOUT_SECS", 0)  # do not wait for the sampler
    dirs = tasks.RunDirs(tmp_path / "r")
    dirs.repo.mkdir()
    go = tmp_path / "go"
    arm = "import os, time\n" f"while not os.path.exists({str(go)!r}):\n" "    time.sleep(0.05)\n"
    release = threading.Event()

    def sample() -> bool:
        go.touch()  # lets the arm exit, then stays busy until the test releases it
        release.wait()
        return False

    try:
        ran = tasks.drive_arm([sys.executable, "-c", arm], dirs, {}, default_timeout() / 2, sample)
        assert not release.is_set()  # returned while the sample was still running
        assert ran.returncode == 0 and not ran.killed and ran.first_pass_s is None
    finally:
        release.set()


def _rec(case, verdict, wall=10.0, cost=1.0, asks=0, idle=2.0, arm="claude-code", tier="simple"):
    return {
        "case": case,
        "arm": arm,
        "tier": tier,
        "verdict": verdict,
        "wall_s": wall,
        "cost": cost,
        "cost_unit": "usd",
        "asks": asks,
        "idle_after_done_s": idle,
    }


def test_summary_counts_pass_k_per_case_and_leaves_void_runs_out_of_the_medians():
    records = [
        _rec("a", "PASSED", wall=10),
        _rec("a", "PASSED", wall=30),
        _rec("b", "PASSED", wall=20),
        _rec("b", "FAILED", wall=40, asks=1),
        _rec("b", "VOID", wall=0, cost=None, idle=None),
        # A VOID run leaves its case short of k runs, so it is not a pass^k case.
        _rec("c", "PASSED", wall=10),
        _rec("c", "VOID", wall=0, cost=None, idle=None),
        _rec("a", "PASSED", arm="crew-chat"),
    ]
    rows = tasks.summarize(records, {"a": 2, "b": 3, "c": 2})
    assert [(r["arm"], r["tier"]) for r in rows] == [
        ("claude-code", "simple"),
        ("crew-chat", "simple"),
    ]
    cc = rows[0]
    assert (cc["runs"], cc["passed"], cc["failed"], cc["void"]) == (7, 4, 1, 2)
    assert (cc["cases"], cc["cases_pass_k"], cc["runs_with_asks"]) == (3, 1, 1)
    assert (cc["median_wall_s"], cc["median_cost"], cc["cost_unit"]) == (20.0, 1.0, "usd")
    assert "| claude-code | simple | 7 | 4 | 1 | 2 | 1 | 1/3 |" in tasks.format_summary(rows)
    # The columns are medians, as the JSON fields say.
    assert "| median wall s | median cost | median idle s |" in tasks.format_summary(rows)
    # A batch that stopped before all of a case's runs leaves it incomplete.
    assert tasks.summarize(records, {"a": 5, "b": 3, "c": 2})[0]["cases_pass_k"] == 0


def test_requested_runs_are_the_repeat_or_the_tier_k():
    cases = tasks.find_cases(SUITE, "all", [])
    assert tasks.requested_runs(cases, None) == {"S1-image-name-cap": 5, "M1-ics-feed": 3}
    assert tasks.requested_runs(cases, 2) == {"S1-image-name-cap": 2, "M1-ics-feed": 2}


def test_a_relative_package_path_is_resolved_before_children_change_directory(tmp_path):
    dirs = tasks.RunDirs(tmp_path / "r")
    source = {"PATH": "/bin", "PYTHONPATH": os.pathsep.join(["src", "/abs/lib"])}
    env = tasks.run_env(dirs, ARMS["crew-chat"], source)
    assert env["PYTHONPATH"] == os.pathsep.join([os.path.abspath("src"), "/abs/lib"])


def test_the_audit_matches_the_suite_path_only_as_a_whole_component(tmp_path):
    dirs = tasks.RunDirs(tmp_path / "r")
    (dirs.out / "arm.stdout").write_text(f'{{"cwd": "{SUITE}-runs/r1/repo"}}\n')
    assert tasks.audit_run(dirs, SUITE) == ""
    (dirs.out / "arm.stdout").write_text(f"read {SUITE}/cases/simple/x/ref/a.py\n")
    assert tasks.audit_run(dirs, SUITE) == "audit: arm.stdout names the case suite path"


# ---------- one run, end to end ----------
def _fake_claude(bin_dir: Path, mode: str, signal_dir: Path) -> None:
    """A ``claude`` that does what *mode* says in its cwd, then prints a stream-json result.

    ``fix_slow`` stays up until the runner's sampler has seen the check pass (the test's
    ``run_check`` spy writes ``sampled`` in *signal_dir*); ``server`` leaves a child
    running and writes its pid to ``server.pid``.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = f"""#!{sys.executable}
import json, os, shutil, subprocess, sys, time
mode = {mode!r}
signal_dir = {str(signal_dir)!r}
if mode in ("fix", "fix_slow", "server"):
    shutil.copy({str(S1 / "ref" / "attach_names.py")!r}, "attach_names.py")
    subprocess.run(["git", "commit", "-qam", "fix"], check=True)
if mode == "fix_slow":
    while not os.path.exists(os.path.join(signal_dir, "sampled")):
        time.sleep(0.05)
if mode == "server":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    open(os.path.join(signal_dir, "server.pid"), "w").write(str(child.pid))
if mode == "ask":
    print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "AskUserQuestion"}}]}}}}))
if mode == "leak":
    print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": {str(SUITE)!r}}}]}}}}))
if mode == "hang":
    time.sleep(30)
if mode == "link":
    os.symlink("/nonexistent/secret", os.path.join("..", "out", "dangling"))
if mode.startswith("plant:"):
    os.symlink(mode.split(":", 1)[1], os.path.join("..", "out", "result.json"))
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.25, "permission_denials": []}}))
"""
    path = bin_dir / "claude"
    path.write_text(script)
    path.chmod(0o755)


def _run_claude(tmp_path, monkeypatch, mode, caps=None):
    _fake_claude(tmp_path / "bin", mode, tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(tasks, "SAMPLE_SECS", 0)
    caps = caps or tasks.Caps(default_timeout() / 2, 30, 3)
    monkeypatch.setattr(tasks.Case, "caps", lambda self: caps)
    real_check = tasks.run_check

    def run_check(case, origin):
        verdict = real_check(case, origin)
        if verdict["pass"]:
            (tmp_path / "sampled").touch()
        return verdict

    monkeypatch.setattr(tasks, "run_check", run_check)
    case = tasks.load_case(S1)
    root, out = tmp_path / "evalroot", tmp_path / "results"
    root.mkdir()
    record = tasks.run_one(case, ARMS["claude-code"], 1, root, out, SUITE, "", dict(os.environ))
    return record, root, out


def test_a_claude_run_that_commits_the_fix_passes_and_leaves_one_result(tmp_path, monkeypatch):
    record, root, out = _run_claude(tmp_path, monkeypatch, "fix_slow")
    assert (record["verdict"], record["reason"], record["asks"]) == ("PASSED", "", 0)
    # Refused tools and an unknown cost's floor are kept on the record, apart from asks.
    assert (record["denials"], record["cost_floor"]) == ([], None)
    assert (record["cost"], record["cost_unit"]) == (0.25, "usd")
    assert record["credentials"] == "eval-only"
    assert record["check"]["pass"] is True
    # The agent only committed: the runner synced trunk to the origin the check judged.
    # Sampled while the arm still ran, so the idle time covers part of its sleep after it.
    assert 0 < record["idle_after_done_s"] <= record["wall_s"]
    saved = json.loads((out / "runs" / record["run_id"] / "result.json").read_text())
    assert saved == record
    assert (out / "runs" / record["run_id"] / "arm.stdout").is_file()
    assert list(root.iterdir()) == []  # the run's eval home is deleted after copy-out


def test_a_run_that_asks_fails_even_with_nothing_else_wrong(tmp_path, monkeypatch):
    record, _, _ = _run_claude(tmp_path, monkeypatch, "ask")
    assert (record["verdict"], record["reason"], record["asks"]) == ("FAILED", "ask", 1)


def test_a_run_that_changes_nothing_fails_the_hidden_check(tmp_path, monkeypatch):
    record, _, _ = _run_claude(tmp_path, monkeypatch, "noop")
    assert (record["verdict"], record["reason"]) == ("FAILED", "hidden_check")
    assert record["idle_after_done_s"] is None


def test_a_run_whose_output_names_the_suite_is_void(tmp_path, monkeypatch):
    record, _, _ = _run_claude(tmp_path, monkeypatch, "leak")
    assert record["verdict"] == "VOID" and record["reason"].startswith("audit:")


def test_an_arm_past_its_cap_is_killed_and_failed(tmp_path, monkeypatch):
    caps = tasks.Caps(timeout_s=0, credits=1, usd=1)
    record, _, _ = _run_claude(tmp_path, monkeypatch, "hang", caps)
    assert (record["verdict"], record["reason"], record["arm_status"]) == (
        "FAILED",
        "timeout",
        "timeout",
    )


def _alive(pid: int) -> bool:
    """A live process; a killed one that no init has reaped yet (a zombie) counts as gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    try:
        return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return not stat.parent.parent.is_dir()  # no /proc (macOS): kill(0) is the answer


def test_a_process_the_agent_left_running_is_killed_after_the_check(tmp_path, monkeypatch):
    record, _, _ = _run_claude(tmp_path, monkeypatch, "server")
    assert record["verdict"] == "PASSED"
    pid = int((tmp_path / "server.pid").read_text())
    wait_until(lambda: not _alive(pid), describe=lambda: f"pid {pid} still alive")


def test_an_arm_not_built_is_void_without_building_anything(tmp_path):
    case = tasks.load_case(S1)
    record = tasks.run_one(
        case, ARMS["codex"], 1, tmp_path / "root", tmp_path / "out", SUITE, "", {}
    )
    assert record["verdict"] == "VOID" and "not available" in record["reason"]
    assert not (tmp_path / "root").exists()


def test_a_crew_run_is_judged_before_its_gateway_stops(tmp_path, monkeypatch):
    events: list[str] = []
    seen: dict[str, object] = {}
    fake_gateway = SimpleNamespace(pid=UNALLOCATABLE_PID)

    def start_gateway(dirs, env):
        seen["config"] = json.loads((dirs.crew / "config.json").read_text())
        events.append("start")
        return fake_gateway, 4321

    real_check = tasks.run_check

    def run_check(case, origin):
        events.append("check")
        return real_check(case, origin)

    def stop_process(proc):
        events.append("stop" if proc is fake_gateway else "stop-arm")

    script = tmp_path / "fake_crew.py"
    script.write_text(
        "import json, os\n"
        f"open({str(tmp_path / 'env.json')!r}, 'w').write(json.dumps(dict(os.environ)))\n"
        "print(json.dumps({'status': 'done', 'asks': 0, 'ask_events': [], 'credits': 2.0,"
        " 'agent': {'name': 'c', 'source': 'default', 'configured': ''}, 'transcript': ''}))\n"
    )

    class FakeCrew(CrewChatArm):
        def argv(self, seed, repo, caps, model, tools):
            return [sys.executable, str(script)]

    monkeypatch.setattr(tasks, "start_gateway", start_gateway)
    monkeypatch.setattr(tasks, "run_check", run_check)
    monkeypatch.setattr(tasks, "stop_process", stop_process)
    root = tmp_path / "root"
    root.mkdir()
    source = {"PATH": os.environ["PATH"], "KIRO_API_KEY": "k", "AWS_SECRET_ACCESS_KEY": "leak"}
    record = tasks.run_one(
        tasks.load_case(S1), FakeCrew(), 1, root, tmp_path / "out", SUITE, "", source
    )
    assert events[0] == "start" and events[-1] == "stop"
    assert events[-2] == "check"
    assert record["verdict"] == "FAILED" and record["reason"] == "hidden_check"
    assert record["agent"] == {"name": "c", "source": "default", "configured": ""}
    assert (record["cost"], record["cost_unit"]) == (2.0, "credits")
    assert seen["config"] == {
        "agent": {
            "sandbox": "strict",
            "sandbox_allow_unsandboxed_exec": False,
            "kiro_hooks_autoimport": False,  # no ~/.kiro/hooks/*.sh script is imported
        }
    }
    env = json.loads((tmp_path / "env.json").read_text())
    assert env["KIROCREW_PORT"] == "4321" and env["KIRO_API_KEY"] == "k"
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["HOME"].startswith(str(root)) and env["KIROCREW_HOME"].startswith(str(root))


def test_a_batch_runs_each_case_its_tier_k_times_by_default(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        tasks, "run_one", lambda case, arm, i, *a: calls.append((case.id, arm.name, i)) or {}
    )
    cases = tasks.find_cases(SUITE, "all", [])
    tasks.run_batch(cases, [ARMS["codex"]], None, tmp_path, tmp_path, SUITE, "", {})
    assert sorted(calls) == sorted(
        [("S1-image-name-cap", "codex", i) for i in range(1, 6)]
        + [("M1-ics-feed", "codex", i) for i in range(1, 4)]
    )
    calls.clear()
    tasks.run_batch(cases, [ARMS["codex"]], 1, tmp_path, tmp_path, SUITE, "", {})
    assert len(calls) == 2


def test_harness_names_resolve_or_are_refused():
    assert [a.name for a in tasks.resolve_harnesses("all")] == list(ARMS)
    assert [a.name for a in tasks.resolve_harnesses("claude-code,crew-chat,claude-code")] == [
        "claude-code",
        "crew-chat",
    ]
    with pytest.raises(ValueError, match="unknown harness 'nope'"):
        tasks.resolve_harnesses("nope")


# ---------- the command ----------
def _parse(argv: list[str]) -> argparse.Namespace:
    root = argparse.ArgumentParser(prog="kirocrew")
    sub = root.add_subparsers(dest="command")
    cli_parser_admin.register_eval_parser(sub, argparse.ArgumentParser(add_help=False))
    return root.parse_args(["eval", *argv])


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["run", "--suite", "tasks"], ""),
        (["smoke_test"], ""),
        (["--suite", "tasks"], "--suite needs kirocrew eval run"),
        (["--check"], "--check needs kirocrew eval run"),
        (["run", "x"], "takes no scenario names"),
        (["run", "--judge"], "does not take --judge"),
        (["run", "--dir", "d"], "does not take --eval-dir"),
        (["run", "--max-drop", "0.1"], "does not take --max-drop"),
    ],
)
def test_the_two_eval_forms_do_not_mix(argv, error):
    found = cli_commands_eval._eval_run_flag_refusal(_parse(argv))
    assert (error in found and bool(found)) if error else found == ""


def test_check_from_the_command_line(monkeypatch, capsys):
    monkeypatch.chdir(SUITE.parents[1])
    assert cli_commands_eval._run_eval_tasks(_parse(["run", "--suite", "tasks", "--check"])) == 0
    assert "2/2 cases bite and are solvable" in capsys.readouterr().out


def test_running_arms_needs_an_eval_home(capsys):
    args = _parse(["run", "--suite", str(SUITE), "--harness", "codex"])
    assert cli_commands_eval._run_eval_tasks(args) == 2
    assert "needs --eval-home" in capsys.readouterr().err


def test_a_refused_preflight_exits_3_before_any_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tasks, "preflight", lambda arms, env: ["no"])
    monkeypatch.setattr(tasks, "run_batch", lambda *a: pytest.fail("ran"))
    args = _parse(
        ["run", "--suite", str(SUITE), "--harness", "codex", "--eval-home", str(tmp_path / "h")]
    )
    assert cli_commands_eval._run_eval_tasks(args) == 3
    assert "Refused: no" in capsys.readouterr().err


def test_a_batch_writes_the_summary_and_exits_by_the_scored_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tasks, "preflight", lambda arms, env: [])
    out = tmp_path / "out"
    argv = ["run", "--suite", str(SUITE), "--harness", "codex", "--tier", "simple", "--repeat", "1"]
    argv += ["--eval-home", str(tmp_path / "h"), "--out", str(out), "--json"]
    # Only a VOID run: nothing scored, so the batch does not claim a pass.
    assert cli_commands_eval._run_eval_tasks(_parse(argv)) == 1
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["summary"][0]["void"] == 1
    summary = json.loads(Path(printed["report"]).read_text())
    assert summary["runs"][0]["verdict"] == "VOID"
    assert sorted(p.suffix for p in out.glob("eval_*")) == [".json", ".md"]


@pytest.mark.parametrize("where", ["ancestor", "inside"])
def test_an_eval_home_around_or_inside_the_suite_is_refused(where, capsys):
    home = SUITE.parents[1] if where == "ancestor" else SUITE / "runs"
    args = _parse(["run", "--suite", str(SUITE), "--harness", "codex", "--eval-home", str(home)])
    assert cli_commands_eval._run_eval_tasks(args) == 2
    assert "must not contain or sit inside the case suite" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="the hook is a POSIX script")
def test_the_runner_syncs_the_committed_branch_whatever_the_agent_set(tmp_path):
    """No arm pushes: the runner fetches trunk into origin, from origin's side, hooks off."""
    case = tasks.load_case(S1)
    repo, origin, elsewhere = tmp_path / "repo", tmp_path / "origin.git", tmp_path / "x.git"
    tasks.build_repo(case, repo, origin)
    env = {"PATH": os.environ["PATH"], **tasks._base_env()}
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(["git", "init", "-q", "--bare", str(elsewhere)], check=True, env=env)
    shutil.copy(S1 / "ref" / "attach_names.py", repo / "attach_names.py")
    for cmd in (
        [*git, "commit", "-qam", "fix"],
        # The agent points its remote elsewhere: the sync must not follow it.
        ["git", "remote", "set-url", "origin", str(elsewhere)],
    ):
        subprocess.run(cmd, cwd=repo, check=True, env=env, capture_output=True)
    marker = tmp_path / "hook-ran"
    hook = origin / "hooks" / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    assert tasks.run_check(case, origin)["pass"] is False  # committed, not yet synced
    assert tasks.sync_task_branch(repo, origin) is True
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, env=env, capture_output=True, encoding="utf-8"
    ).stdout.strip()
    synced = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", f"refs/heads/{tasks.TASK_BRANCH}"],
        env=env,
        capture_output=True,
        encoding="utf-8",
    ).stdout.strip()
    assert synced == head
    assert tasks.run_check(case, origin)["pass"] is True
    assert not marker.exists()  # origin's hooks are off for the sync
    refs = subprocess.run(
        ["git", "--git-dir", str(elsewhere), "for-each-ref"],
        env=env,
        capture_output=True,
        encoding="utf-8",
    )
    assert refs.stdout == ""  # nothing went where the agent pointed
    # A repo without the branch is not an error that ends the batch.
    assert tasks.sync_task_branch(tmp_path / "missing", origin) is False
    # The agent deletes trunk after a sample synced a passing commit: the run's own
    # check fails instead of grading the earlier snapshot still in origin.
    dirs = SimpleNamespace(repo=repo, origin=origin)
    subprocess.run(["git", "checkout", "-q", "-b", "other"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "branch", "-q", "-D", "trunk"], cwd=repo, check=True, env=env)
    verdict = tasks._synced_check(case, dirs)
    assert verdict["pass"] is False and "could not be synced" in verdict["notes"][0]


def test_a_crew_run_keeps_every_chat_transcript_and_agent_record(tmp_path):
    dirs = tasks.RunDirs(tmp_path / "r")
    sessions, agents = dirs.crew / "sessions", dirs.crew / "subagents" / "a1"
    sessions.mkdir()
    agents.mkdir(parents=True)
    root = sessions / "dashboard_chat-1-1.jsonl"
    root.write_text('{"role": "user"}\n')
    (sessions / "dashboard_chat-2-2.jsonl").write_text('{"role": "tool"}\n')  # a worker
    (agents / "result.txt").write_text("done\n")
    secret = tmp_path / "outside.txt"
    secret.write_text("not the run's")
    if sys.platform != "win32":
        (sessions / "link.jsonl").symlink_to(secret)
    tasks._copy_transcript(json.dumps({"transcript": str(root)}) + "\n", dirs)
    assert (dirs.out / "transcript.jsonl").read_text() == '{"role": "user"}\n'
    kept = dirs.out / "crew"
    assert (kept / "sessions" / "dashboard_chat-2-2.jsonl").read_text() == '{"role": "tool"}\n'
    assert (kept / "subagents" / "a1" / "result.txt").read_text() == "done\n"
    if sys.platform != "win32":
        assert (kept / "sessions" / "link.jsonl").is_symlink()  # copied as a link, not read


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privilege on Windows")
def test_a_link_the_agent_left_at_the_log_destination_is_replaced_not_followed(tmp_path):
    dirs = tasks.RunDirs(tmp_path / "r")
    (dirs.crew / "sessions").mkdir()
    (dirs.crew / "sessions" / "security_policy.json").write_text('{"planted": true}')
    target = tmp_path / "victim"
    target.mkdir()
    (target / "security_policy.json").write_text('{"real": true}')
    (dirs.out / "crew").symlink_to(target)
    tasks._copy_transcript("", dirs)
    assert (target / "security_policy.json").read_text() == '{"real": true}'
    assert not (dirs.out / "crew").is_symlink()
    assert (dirs.out / "crew" / "sessions" / "security_policy.json").is_file()


def test_a_submission_that_raises_its_own_limit_still_fails_the_check(tmp_path):
    case = tasks.load_case(S1)
    repo, origin = tmp_path / "repo", tmp_path / "origin.git"
    tasks.build_repo(case, repo, origin)
    module = repo / "attach_names.py"
    module.write_text(module.read_text().replace("MAX_NAME_CHARS = 64", "MAX_NAME_CHARS = 1000"))
    env = {"PATH": os.environ["PATH"]}
    for cmd in (["git", "commit", "-qam", "raise"], ["git", "push", "-q", "origin", "trunk"]):
        subprocess.run(cmd, cwd=repo, check=True, env=env, capture_output=True)
    verdict = tasks.run_check(case, origin)
    assert verdict["pass"] is False
    assert verdict["parts"]["test_a_long_image_name_keeps_its_extension"] is False


def test_an_interrupted_arm_is_stopped_before_the_interrupt_propagates(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "SAMPLE_SECS", 0)
    dirs = tasks.RunDirs(tmp_path / "r")
    dirs.repo.mkdir()
    pid_file = tmp_path / "arm.pid"
    arm = (
        "import os, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(600)\n"
    )

    class InterruptedWait(subprocess.Popen):
        """Ctrl-C lands while the runner waits on the arm (the first, deadline wait)."""

        interrupted = False

        def wait(self, timeout=None):
            if not InterruptedWait.interrupted:
                InterruptedWait.interrupted = True
                wait_until(lambda: pid_file.exists(), describe=lambda: "no arm pid")
                raise KeyboardInterrupt
            return super().wait(timeout)

    proxy = SimpleNamespace(
        Popen=InterruptedWait,
        DEVNULL=subprocess.DEVNULL,
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    monkeypatch.setattr(tasks, "subprocess", proxy)
    with pytest.raises(KeyboardInterrupt):
        tasks.drive_arm([sys.executable, "-c", arm], dirs, {}, default_timeout() / 2, lambda: False)
    pid = int(pid_file.read_text())
    wait_until(lambda: not _alive(pid), describe=lambda: f"arm {pid} still alive")


def test_the_kill_deadline_fires_while_a_check_sample_is_still_running(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "SAMPLE_SECS", 0)
    monkeypatch.setattr(tasks, "KILL_GRACE_SECS", 0)
    monkeypatch.setattr(tasks, "CHECK_TIMEOUT_SECS", 0)  # do not wait for the sampler
    dirs = tasks.RunDirs(tmp_path / "r")
    dirs.repo.mkdir()
    release = threading.Event()

    def sample() -> bool:
        release.wait()  # a sample that never finishes on its own
        return False

    try:
        arm = [sys.executable, "-c", "import time; time.sleep(600)"]
        ran = tasks.drive_arm(arm, dirs, {}, 0, sample)
        assert ran.killed and not release.is_set()
    finally:
        release.set()


def test_a_process_the_checked_code_starts_dies_with_the_check(tmp_path):
    case_dir = tmp_path / "case"
    (case_dir / "hidden").mkdir(parents=True)
    pid_file = tmp_path / "child.pid"
    (case_dir / "hidden" / "check.py").write_text(
        "import json, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'],\n"
        "                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "print(json.dumps({'pass': True, 'parts': {}, 'notes': []}))\n"
    )
    case = tasks.Case("c", 1, "simple", "{repo}", 1, 1, 1, [], case_dir)
    assert tasks.run_check(case, tmp_path / "origin.git")["pass"] is True
    pid = int(pid_file.read_text())
    wait_until(lambda: not _alive(pid), describe=lambda: f"check child {pid} still alive")


def test_the_credential_list_is_the_whole_sandbox_list():
    from kiro_crew.security.paths import sensitive_home_dirs

    paths = tasks.credential_paths()
    for name in (".aws", ".ssh", ".config/gh", ".kube", ".netrc", ".git-credentials"):
        assert name in paths
    assert set(sensitive_home_dirs()) <= set(paths)


def test_a_crew_credential_leaf_refuses_unless_it_is_an_empty_directory(tmp_path):
    home = tmp_path / "eval"
    (home / ".kiro" / "crew").mkdir(parents=True)
    (home / ".kiro" / "crew" / ".env").write_text("SLACK_TOKEN=x")
    assert tasks._holds_credentials(str(home), ".kiro/crew/.env")
    (home / ".kiro" / "crew" / "diag").mkdir()
    assert not tasks._holds_credentials(str(home), ".kiro/crew/diag")
    (home / ".kiro" / "crew" / "diag" / "dump").write_text("")
    assert tasks._holds_credentials(str(home), ".kiro/crew/diag")
    (home / ".ssh").mkdir()  # outside Crew's data home even an empty dir refuses
    assert tasks._holds_credentials(str(home), ".ssh")


def test_a_run_that_changes_the_suite_is_void_and_stops_the_batch(tmp_path, monkeypatch):
    suite = tmp_path / "suite"
    shutil.copytree(SUITE, suite)
    cases = tasks.find_cases(suite, "all", [])
    calls = []

    def run_one(case, arm, index, eval_root, out_dir, suite_dir, model, env):
        calls.append(case.id)
        if len(calls) == 1:  # the first run rewrites a grader
            check = suite_dir / "cases" / "simple" / "S1-image-name-cap" / "hidden" / "check.py"
            check.write_text(check.read_text() + "\n# rewritten\n")
        return {"run_id": f"r{len(calls)}", "verdict": "PASSED", "reason": ""}

    monkeypatch.setattr(tasks, "run_one", run_one)
    records = tasks.run_batch(cases, [ARMS["codex"]], 1, tmp_path, tmp_path / "out", suite, "", {})
    assert len(calls) == 1
    assert (records[0]["verdict"], records[0]["reason"]) == (
        "VOID",
        "audit: the case suite changed during this run",
    )
    saved = json.loads((tmp_path / "out" / "runs" / "r1" / "result.json").read_text())
    assert saved["verdict"] == "VOID"


def test_running_a_check_does_not_change_the_suite_fingerprint(tmp_path):
    suite = tmp_path / "suite"
    shutil.copytree(SUITE, suite)
    before = tasks.suite_digest(suite)
    assert tasks.check_cases(tasks.find_cases(suite, "simple", [])) == []
    assert tasks.suite_digest(suite) == before  # bytecode caches are left out


@pytest.mark.parametrize("where", ["suite", "inside"])
def test_an_out_dir_that_is_or_sits_inside_the_suite_is_refused(
    where, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(tasks, "run_batch", lambda *a: pytest.fail("ran"))
    out = SUITE if where == "suite" else SUITE / "results"
    argv = ["run", "--suite", str(SUITE), "--harness", "codex"]
    argv += ["--eval-home", str(tmp_path / "h"), "--out", str(out)]
    assert cli_commands_eval._run_eval_tasks(_parse(argv)) == 2
    assert "--out must not be the case suite or sit inside it" in capsys.readouterr().err
    assert not (SUITE / "results").exists()


def test_a_link_the_agent_leaves_in_the_run_output_does_not_break_the_batch(tmp_path, monkeypatch):
    record, root, out = _run_claude(tmp_path, monkeypatch, "link")
    assert record["verdict"] == "FAILED" and record["reason"] == "hidden_check"
    copied = out / "runs" / record["run_id"] / "dangling"
    assert copied.is_symlink() and not copied.exists()  # copied as a link, never followed
    assert list(root.iterdir()) == []  # the run directory is still deleted


def test_running_the_checks_writes_no_bytecode_into_the_suite(tmp_path):
    suite = tmp_path / "suite"
    shutil.copytree(SUITE, suite, ignore=shutil.ignore_patterns("__pycache__"))
    assert tasks.check_cases(tasks.find_cases(suite, "all", [])) == []
    assert list(suite.rglob("__pycache__")) == []


def test_a_result_json_link_the_agent_plants_writes_no_other_file(tmp_path, monkeypatch):
    victim = tmp_path / "saved-report.json"
    victim.write_text("keep me")
    record, _, out = _run_claude(tmp_path, monkeypatch, f"plant:{victim}")
    result = out / "runs" / record["run_id"] / "result.json"
    assert not result.is_symlink() and json.loads(result.read_text()) == record
    assert victim.read_text() == "keep me"


def test_a_record_replaces_a_link_at_its_name_instead_of_following_it(tmp_path):
    victim = tmp_path / "victim.json"
    victim.write_text("keep me")
    run_dir = tmp_path / "out" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").symlink_to(victim)
    tasks._write_record(tmp_path / "out", {"run_id": "r1", "verdict": "VOID"})
    assert victim.read_text() == "keep me"
    assert not (run_dir / "result.json").is_symlink()
    assert [p.name for p in run_dir.iterdir()] == ["result.json"]  # no temp file left


def test_a_planted_bytecode_cache_changes_the_suite_fingerprint(tmp_path):
    suite = tmp_path / "suite"
    shutil.copytree(SUITE, suite, ignore=shutil.ignore_patterns("__pycache__"))
    before = tasks.suite_digest(suite)
    cache = suite / "lib" / "__pycache__"
    cache.mkdir()
    (cache / "hidden.cpython-312.pyc").write_bytes(b"forged")
    assert tasks.suite_digest(suite) != before


def test_a_cache_in_the_suite_is_never_read_by_the_check(tmp_path):
    """Even a cache that matches lib/hidden.py's stamp is ignored: a private prefix is used."""
    import py_compile

    suite = tmp_path / "suite"
    shutil.copytree(SUITE, suite, ignore=shutil.ignore_patterns("__pycache__"))
    forged = tmp_path / "forged_hidden.py"
    forged.write_text(
        "import json\n"
        "def main(namespace):\n"
        "    print(json.dumps({'pass': True, 'parts': {}, 'counters': {}, 'notes': []}))\n"
    )
    real = suite / "lib" / "hidden.py"
    cache = suite / "lib" / "__pycache__"
    cache.mkdir()
    target = cache / f"hidden.{sys.implementation.cache_tag}.pyc"
    py_compile.compile(
        str(forged),
        cfile=str(target),
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    assert real.exists()
    case = tasks.load_case(suite / "cases" / "simple" / "S1-image-name-cap")
    repo, origin = tmp_path / "repo", tmp_path / "origin.git"
    tasks.build_repo(case, repo, origin)
    assert tasks.run_check(case, origin)["pass"] is False  # the real grader ran on the base


def test_an_administrator_home_the_eval_user_can_list_is_refused(monkeypatch, tmp_path):
    mine, root_home = tmp_path / "eval", tmp_path / "root"
    mine.mkdir()
    root_home.mkdir()
    _fake_users(monkeypatch, _pw("eval", 4000, mine), [_pw("root", 0, root_home)])
    assert tasks.preflight([ARMS["codex"]], {}) == [
        f"root's home {root_home} is readable by the eval user"
    ]


def test_a_transcript_link_the_agent_plants_writes_no_other_file(tmp_path):
    dirs = tasks.RunDirs(tmp_path / "r")
    victim = tmp_path / "saved-report.json"
    victim.write_text("keep me")
    (dirs.out / "transcript.jsonl").symlink_to(victim)
    transcript = dirs.crew / "sessions" / "dashboard_s1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"role": "user"}\n')
    tasks._copy_transcript(json.dumps({"transcript": str(transcript)}) + "\n", dirs)
    assert victim.read_text() == "keep me"
    copied = dirs.out / "transcript.jsonl"
    assert not copied.is_symlink() and copied.read_text() == '{"role": "user"}\n'


# ---------- --use-operator-credentials ----------
def _as_operator(*names):
    import copy

    out = []
    for name in names:
        arm = copy.copy(ARMS[name])
        arm.operator_credentials = True
        out.append(arm)
    return out


def test_operator_credentials_skip_the_eval_user_checks_and_ask_each_sign_in(monkeypatch, tmp_path):
    from kiro_crew import sandbox

    mine = tmp_path / "me"
    (mine / ".ssh").mkdir(parents=True)  # the operator's own credentials are the point
    theirs = tmp_path / "alice"
    theirs.mkdir()
    _fake_users(monkeypatch, _pw("me", 4000, mine), [_pw("alice", 4242, theirs)])
    monkeypatch.setattr(sandbox, "detect_backend", lambda mode="auto": "bwrap")
    chat, conductor, cc = _as_operator("crew-chat", "crew-conductor", "claude-code")
    asked = []
    for arm, answer in ((chat, ""), (conductor, ""), (cc, "")):
        monkeypatch.setattr(
            arm, "operator_check", lambda env, a=arm, r=answer: asked.append(a.name) or r
        )
    monkeypatch.setattr(cc, "unavailable", lambda: "")
    not_built = _as_operator("kiro-cli-v3", "codex")  # `--harness all` includes them
    assert tasks.preflight([chat, conductor, cc, *not_built], {}) == []
    assert asked == ["crew-chat", "claude-code"]  # one `kiro-cli whoami` for both Crew arms
    monkeypatch.setattr(cc, "operator_check", lambda env: '`claude -p "say ok"` failed')
    assert tasks.preflight([cc], {}) == ['claude-code: `claude -p "say ok"` failed']
    # Without the flag, the eval-user checks are exactly as before.
    assert tasks.preflight([ARMS["codex"]], {}) == [
        f"{mine}/.ssh exists: run the eval as a dedicated user with no credentials",
        f"alice's home {theirs} is readable by the eval user",
    ]


def test_operator_credentials_keep_the_runners_env_and_home_but_a_run_data_home(
    monkeypatch, tmp_path
):
    from kiro_crew import kiro_cli

    # The binary preflight checked is pinned for the run, even when only the
    # operator's own KIROCREW_KIRO_BIN (dropped with the other KIROCREW_* keys) names it.
    monkeypatch.setattr(
        kiro_cli, "resolve_kiro_cli", lambda environ=None: environ.get("KIROCREW_KIRO_BIN")
    )
    (cc,) = _as_operator("claude-code")
    dirs = tasks.RunDirs(tmp_path / "r")
    source = {
        "PATH": "/bin",
        "HOME": "/me",
        "AWS_PROFILE": "p",
        "KIROCREW_PORT": "9",
        "KIROCREW_KIRO_BIN": "/opt/kiro/kiro-cli",
        "PYTHONPATH": "src",
    }
    assert tasks.run_env(dirs, cc, source) == {
        "PATH": "/bin",
        "HOME": "/me",
        "AWS_PROFILE": "p",
        "PYTHONPATH": os.path.abspath("src"),  # children start in other directories
        "KIROCREW_KIRO_BIN": os.path.abspath("/opt/kiro/kiro-cli"),
        "KIROCREW_HOME": str(dirs.crew),
        "KIRO_HOME": str(dirs.kiro),
        "TMPDIR": str(dirs.tmp),
    }
    # A relative override is pinned absolute: the gateway starts in the run's home.
    relative = {**source, "KIROCREW_KIRO_BIN": "bin/kiro-cli"}
    pinned = tasks.run_env(tasks.RunDirs(tmp_path / "r2"), cc, relative)["KIROCREW_KIRO_BIN"]
    assert pinned == os.path.abspath("bin/kiro-cli")


def test_operator_credentials_from_the_command_line_print_the_risk_and_swap_copies(
    tmp_path, monkeypatch, capsys
):
    seen = {}
    monkeypatch.setattr(tasks, "preflight", lambda arms, env: seen.update(arms=arms) or ["no"])
    args = _parse(
        [
            "run",
            "--suite",
            str(SUITE),
            "--harness",
            "crew-chat,claude-code",
            "--use-operator-credentials",
            "--eval-home",
            str(tmp_path / "h"),
        ]
    )
    assert cli_commands_eval._run_eval_tasks(args) == 3
    chat, cc = seen["arms"]
    assert chat.operator_credentials and cc.operator_credentials
    assert not ARMS["claude-code"].operator_credentials  # the shared instances are untouched
    assert tasks.OPERATOR_RISK_LINE in capsys.readouterr().err
