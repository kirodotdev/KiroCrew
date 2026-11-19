"""The ``tasks`` suite of ``kirocrew eval run``: real coding cases, each run unattended.

One code path for every tier (rfc-builtin-eval.md §3.3): build the case's repo
with a local origin, start the arm on the seed inside a fresh eval home, sample
the hidden check while it runs, run the check once more when it ends, and score
the run PASSED, FAILED or VOID (§5). Tiers differ only in data (:data:`TIERS`
and the case's own ``case.json``).

The fence is the eval home (§4.1): every process of a run gets ``HOME``,
``KIROCREW_HOME``, ``KIRO_HOME`` and ``TMPDIR`` inside one run directory, an env
with only the arm's eval-only sign-in, and, for Crew arms, the ``strict``
sandbox tier with no unsandboxed fallback. :func:`preflight` refuses before any
run when the user running the eval has credential directories, can list another
person's home, has no sandbox backend, or lacks an arm's eval sign-in. A Crew
arm's gateway is stopped only after the final check has run, so a server the
agent started is still alive while it is judged.

POSIX only in this version: the home check reads the password database and the
cases build with ``sh``. Windows is refused at preflight.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from kiro_crew import platform_compat
from kiro_crew.eval.arms import ARMS, Arm, ArmOutcome, Caps

#: Per-tier limits and weekly runs per case (§3.1): timeout, credit and USD caps, k.
TIERS: dict[str, dict[str, float]] = {
    "simple": {"timeout_minutes": 20, "credits": 30, "usd": 3, "k": 5},
    "medium": {"timeout_minutes": 120, "credits": 120, "usd": 12, "k": 3},
    "complex": {"timeout_minutes": 240, "credits": 400, "usd": 40, "k": 2},
}
#: Branch every built task repo uses. Not ``main``: Kiro Crew refuses a push to a
#: protected branch name even to a local origin, so a Crew arm could not finish.
TASK_BRANCH = "trunk"
#: Seconds between hidden-check samples while an arm runs (idle-after-done, §10).
SAMPLE_SECS = 60.0
#: Seconds past the cap before the runner kills an arm that ignored its own cap.
KILL_GRACE_SECS = 60.0
#: Seconds a case build (or the reference push of ``--check``) may take.
BUILD_TIMEOUT_SECS = 300.0
#: Seconds one hidden-check run may take.
CHECK_TIMEOUT_SECS = 300.0
#: Seconds a gateway may take to become ready.
GATEWAY_READY_SECS = 180.0
#: Credential directories the eval user's own home must not hold (§4.1), besides
#: every path the sandbox hides as a credential (``credential_paths``).
CREDENTIAL_DIRS = (".aws", ".ssh", ".config/gh", ".kube")


def credential_paths() -> tuple[str, ...]:
    """Home-relative paths whose presence refuses the eval user (§4.1).

    The sandbox's own credential list (``security.paths.sensitive_home_dirs``) plus
    :data:`CREDENTIAL_DIRS`; see :func:`_holds_credentials` for the one exception.
    """
    from kiro_crew.security.paths import sensitive_home_dirs

    return tuple(dict.fromkeys([*CREDENTIAL_DIRS, *sensitive_home_dirs()]))


def _holds_credentials(home: str, name: str) -> bool:
    """Whether ``home/name`` exists and may hold a credential.

    An EMPTY directory under Crew's own data home is the one exception: running
    ``kirocrew eval run`` bootstraps the eval user's ``~/.kiro/crew`` and may create
    such a directory before anything is in it. Any file, and any non-empty
    directory, refuses (``.kiro/crew/.env`` included).
    """
    path = os.path.join(home, name)
    if not os.path.lexists(path):
        return False
    if name.startswith((".kiro/crew/", ".kiro/crew-")) and os.path.isdir(path):
        try:
            return any(os.scandir(path))
        except OSError:
            return True
    return True


#: Env variables every run process keeps; everything else is dropped.
_BASE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ", "PYTHONPATH")
_DEFAULT_TOOLS = ["Read", "Edit", "Write", "Bash", "Glob", "Grep"]


class CaseError(ValueError):
    """A case directory whose ``case.json`` breaks the contract."""


@dataclass
class Case:
    id: str
    version: int
    tier: str
    seed: str
    timeout_minutes: float
    credits: float
    usd: float
    tools: list[str]
    path: Path

    def caps(self) -> Caps:
        tier = TIERS[self.tier]
        return Caps(
            timeout_s=60 * min(self.timeout_minutes, tier["timeout_minutes"]),
            credits=min(self.credits, tier["credits"]),
            usd=min(self.usd, tier["usd"]),
        )


def load_case(path: Path) -> Case:
    """Read and check ``path/case.json`` and the files the runner needs beside it."""
    try:
        data = json.loads((path / "case.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CaseError(f"{path.name}: unreadable case.json: {exc}") from None
    if not isinstance(data, dict):
        raise CaseError(f"{path.name}: case.json is not an object")
    tier = data.get("tier")
    if tier not in TIERS:
        raise CaseError(f"{path.name}: tier must be one of {', '.join(TIERS)}")
    if path.parent.name != tier:
        raise CaseError(f"{path.name}: sits under {path.parent.name}/ but says tier {tier}")
    if data.get("id") != path.name:
        raise CaseError(f"{path.name}: id {data.get('id')!r} does not match its directory")
    seed = data.get("seed")
    if not isinstance(seed, str) or "{repo}" not in seed:
        raise CaseError(f"{path.name}: seed must be a string holding {{repo}}")
    if seed.lstrip()[:1] in ("/", "@"):
        raise CaseError(
            f"{path.name}: a seed may not open with / or @ (the headless CLI refuses it)"
        )
    if data.get("kind") != "code":
        raise CaseError(f"{path.name}: only kind 'code' is supported")
    spend = data.get("spend_cap") or {}
    try:
        case = Case(
            id=data["id"],
            version=int(data["version"]),
            tier=tier,
            seed=seed,
            timeout_minutes=float(data["timeout_minutes"]),
            credits=float(spend["credits"]),
            usd=float(spend["usd"]),
            tools=[str(t) for t in data.get("tools") or _DEFAULT_TOOLS],
            path=path,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CaseError(f"{path.name}: missing or bad field: {exc}") from None
    limits = TIERS[tier]
    if (
        case.timeout_minutes > limits["timeout_minutes"]
        or case.credits > limits["credits"]
        or case.usd > limits["usd"]
    ):
        raise CaseError(f"{path.name}: its caps exceed the {tier} tier's")
    for needed in ("build.sh", "hidden/check.py", "ref"):
        if not (path / needed).exists():
            raise CaseError(f"{path.name}: {needed} is missing")
    return case


def find_cases(suite_dir: Path, tier: str, ids: list[str]) -> list[Case]:
    """The cases under ``suite_dir/cases/<tier>/``, filtered by *tier* and *ids*."""
    tiers = list(TIERS) if tier == "all" else [tier]
    cases = [
        load_case(path)
        for name in tiers
        for path in sorted((suite_dir / "cases" / name).glob("*"))
        if path.is_dir()
    ]
    if ids:
        unknown = sorted(set(ids) - {c.id for c in cases})
        if unknown:
            raise CaseError(f"no such case: {', '.join(unknown)}")
        cases = [c for c in cases if c.id in ids]
    return cases


# ---------- building and checking ----------
def build_repo(case: Case, repo: Path, origin: Path) -> None:
    """Run the case's ``build.sh``: a fresh repo at the base, with a local origin."""
    proc = subprocess.run(
        ["sh", str(case.path / "build.sh"), str(repo), str(origin)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=BUILD_TIMEOUT_SECS,
        env=_base_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"build.sh failed: {proc.stderr.strip()[-500:]}")


def run_check(case: Case, origin: Path) -> dict[str, Any]:
    """Run the case's hidden check on *origin*'s task branch; its JSON verdict.

    The check imports the agent's code, so it runs in a process group of its own,
    and that whole group is killed when the check ends, times out or raises: a
    process the submitted code started does not outlive the check.
    """
    with tempfile.TemporaryDirectory(prefix="eval-pyc-") as pyc:
        return _run_check(case, origin, pyc)


def _run_check(case: Case, origin: Path, pyc: str) -> dict[str, Any]:
    proc = subprocess.Popen(
        # -B writes no bytecode, and the private pycache prefix means a cache file in
        # the suite (one the agent could plant) is never read either.
        [
            sys.executable,
            "-B",
            "-X",
            f"pycache_prefix={pyc}",
            str(case.path / "hidden" / "check.py"),
            str(origin),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_base_env(),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=CHECK_TIMEOUT_SECS)
    except subprocess.TimeoutExpired:
        # Kill the whole group before draining: a descendant holding the pipes would
        # otherwise keep the drain waiting, and the leader may already be gone.
        kill_group(proc.pid)
        stop_process(proc)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return {"pass": False, "parts": {}, "notes": ["hidden check timed out"]}
    finally:
        kill_group(proc.pid)
    for line in reversed(stdout.splitlines()):
        try:
            verdict = json.loads(line)
        except ValueError:
            continue
        if isinstance(verdict, dict):
            return {
                "pass": verdict.get("pass") is True,
                "parts": verdict.get("parts") or {},
                "notes": verdict.get("notes") or [],
            }
    return {"pass": False, "parts": {}, "notes": [f"no verdict: {stderr.strip()[-300:]}"]}


def apply_reference(case: Case, repo: Path) -> None:
    """Copy the case's ``ref/`` over *repo*, commit, and push the task branch."""
    shutil.copytree(case.path / "ref", repo, dirs_exist_ok=True)
    env = _base_env()
    for cmd in (
        ["git", "add", "-A"],
        ["git", "commit", "-q", "--allow-empty", "-m", "reference"],
        # Local transport drops GIT_CONFIG_* before receive-pack, so name the setting here.
        [
            "git",
            "push",
            "--receive-pack=git -c maintenance.auto=false receive-pack",
            "-q",
            "origin",
            TASK_BRANCH,
        ],
    ):
        subprocess.run(
            cmd, cwd=repo, check=True, capture_output=True, env=env, timeout=BUILD_TIMEOUT_SECS
        )


def check_cases(cases: list[Case]) -> list[str]:
    """The ``--check`` gate (no model, no tokens): each case bites and is solvable.

    Bites: the hidden check fails on the untouched base. Solvable: it passes once
    the reference (the merged code) is pushed (§13.3 gates 1 and 2).
    """
    problems: list[str] = []
    for case in cases:
        with tempfile.TemporaryDirectory(prefix="eval-case-") as tmp:
            repo, origin = Path(tmp) / "repo", Path(tmp) / "origin.git"
            try:
                build_repo(case, repo, origin)
                if run_check(case, origin)["pass"]:
                    problems.append(f"{case.id}: the hidden check passes on the untouched base")
                apply_reference(case, repo)
                after = run_check(case, origin)
                if not after["pass"]:
                    problems.append(
                        f"{case.id}: the hidden check fails on the reference: {after['notes']}"
                    )
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                problems.append(f"{case.id}: {exc}")
    return problems


# ---------- preflight ----------
def preflight(arms: list[Arm], env: dict[str, str]) -> list[str]:
    """Every reason the batch must not start (§4.1, §10); empty when it may."""
    if os.name != "posix":
        return ["kirocrew eval run --suite tasks runs on Linux and macOS only"]
    reasons = list(_home_refusals())
    if any(arm.needs_gateway for arm in arms):
        from kiro_crew import sandbox

        if sandbox.detect_backend("strict") == "none":
            reasons.append("no sandbox backend: a Crew arm needs the strict sandbox tier")
    for arm in arms:
        if not arm.unavailable():
            missing = arm.missing_credential(env)
            if missing:
                reasons.append(f"{arm.name}: {missing}")
    return reasons


def _home_refusals() -> list[str]:
    """The eval user holds no credential directory and cannot list anyone else's home."""
    import pwd

    me = pwd.getpwuid(platform_compat.effective_uid() or 0)
    reasons = [
        f"{me.pw_dir}/{name} exists: run the eval as a dedicated user with no credentials"
        for name in credential_paths()
        if _holds_credentials(me.pw_dir, name)
    ]
    for entry in pwd.getpwall():
        # 500, not 1000: macOS numbers login users from 501. A Linux service
        # account in 500-999 with a login shell and a listable home is refused
        # too, which errs on the safe side. uid 0 is checked as well: an
        # administrator home the eval user can list fails the fence the same way.
        if (
            (0 < entry.pw_uid < 500)
            or entry.pw_uid == me.pw_uid
            or entry.pw_dir in ("", "/", me.pw_dir)
        ):
            continue
        if os.path.basename(entry.pw_shell) in ("nologin", "false", "true"):
            # A service account (nologin, false or true as its shell), not a
            # person who could log in and hold credentials.
            continue
        try:
            os.listdir(entry.pw_dir)
        except OSError:
            continue
        reasons.append(f"{entry.pw_name}'s home {entry.pw_dir} is readable by the eval user")
    return reasons


# ---------- one run ----------
@dataclass
class RunDirs:
    root: Path
    home: Path = field(init=False)
    crew: Path = field(init=False)
    kiro: Path = field(init=False)
    tmp: Path = field(init=False)
    repo: Path = field(init=False)
    origin: Path = field(init=False)
    out: Path = field(init=False)

    def __post_init__(self) -> None:
        from kiro_crew.owner_only_files import mkdirs_owner_only

        for name in ("home", "crew", "kiro", "tmp", "out"):
            path = self.root / name
            if path.exists():
                raise FileExistsError(path)  # a run directory is always fresh
            mkdirs_owner_only(path)
            setattr(self, name, path)
        self.repo = self.root / "repo"
        self.origin = self.root / "origin.git"


def run_env(dirs: RunDirs, arm: Arm, source: dict[str, str]) -> dict[str, str]:
    """The whole env of a run's processes: base keys, the run's homes, the arm's sign-in."""
    env = {k: source[k] for k in _BASE_ENV_KEYS if source.get(k)}
    if env.get("PYTHONPATH"):
        # Children start in other directories, so a relative entry (a source checkout's
        # `src`) is resolved against the runner's own working directory first.
        parts = env["PYTHONPATH"].split(os.pathsep)
        env["PYTHONPATH"] = os.pathsep.join(os.path.abspath(p) for p in parts if p)
    env.update(
        HOME=str(dirs.home),
        KIROCREW_HOME=str(dirs.crew),
        KIRO_HOME=str(dirs.kiro),
        TMPDIR=str(dirs.tmp),
    )
    env.update({k: source[k] for k in arm.env_keys if source.get(k)})
    return env


def write_crew_config(dirs: RunDirs) -> None:
    """The eval home's Crew config: the strict sandbox tier, no unsandboxed fallback."""
    config = {"agent": {"sandbox": "strict", "sandbox_allow_unsandboxed_exec": False}}
    (dirs.crew / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def start_gateway(dirs: RunDirs, env: dict[str, str]) -> tuple[subprocess.Popen[bytes], int]:
    """Start a gateway in the run's data home on a free loopback port; return it and the port.

    The port is read from the gateway's own run marker (``<data home>/run/gateway-<port>.bin``),
    and the gateway counts as ready once ``/api/health`` answers 200 on loopback. Its output
    goes to a file in the run's private ``tmp/``, which is never copied out.
    """
    argv = platform_compat.isolated_python_argv(
        "-P", "-m", "kiro_crew", "gateway", "--port", "auto", "--no-open"
    )
    with open(dirs.tmp / "gateway.log", "wb") as log:
        proc = subprocess.Popen(
            [*argv, "--no-tunnel"],
            cwd=dirs.home,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + GATEWAY_READY_SECS
    try:
        while time.monotonic() < deadline and proc.poll() is None:
            port = _marker_port(dirs.crew)
            if port is not None and _healthy(port):
                return proc, port
            time.sleep(1)
    except BaseException:
        # Its own session, so a Ctrl-C at the terminal never reaches it: stop it here.
        stop_process(proc)
        raise
    stop_process(proc)
    raise RuntimeError("the eval gateway did not become ready")


def _marker_port(crew: Path) -> int | None:
    for marker in sorted((crew / "run").glob("gateway-*.bin")):
        digits = marker.stem.removeprefix("gateway-")
        if digits.isdigit():
            return int(digits)
    return None


def _healthy(port: int) -> bool:
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/api/health")
        return conn.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def _signal_group(pgid: int, sig: int) -> bool:
    """Signal the process group *pgid* (started with ``start_new_session``); False once it is gone."""
    try:
        return platform_compat.kill_process_group(pgid, sig)
    except (OSError, ValueError):  # gone, unsignalable, or no process groups here
        return False


def stop_process(proc: subprocess.Popen[Any]) -> None:
    """Stop *proc* and everything in its process group: TERM, then KILL after 30 s."""
    if proc.poll() is not None:
        return
    for sig, wait in ((platform_compat.SIGTERM, 30.0), (platform_compat.SIGKILL, 10.0)):
        if not _signal_group(proc.pid, sig):
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


@dataclass
class ArmRun:
    returncode: int
    stdout: str
    wall_s: float
    killed: bool
    first_pass_s: float | None
    #: The arm's process group, still holding anything the agent left running.
    pgid: int


def kill_group(pgid: int) -> None:
    """Kill what is left of an arm's process group (a server the agent started)."""
    _signal_group(pgid, platform_compat.SIGKILL)


def drive_arm(
    argv: list[str],
    dirs: RunDirs,
    env: dict[str, str],
    cap_s: float,
    sample: Callable[[], bool],
) -> ArmRun:
    """Run *argv* in the repo until it exits, killing it at the cap plus :data:`KILL_GRACE_SECS`.

    Arm control is one blocking wait whose timeout is the deadline, so the wall time
    and the kill are exact. Sampling the hidden check (every :data:`SAMPLE_SECS`, for
    idle-after-done) runs on a thread of its own: a slow sample can neither delay the
    kill nor change the wall time or the timeout verdict. It is joined only after the
    arm has ended, for at most one check's time limit.
    """
    import threading

    out_path, err_path = dirs.out / "arm.stdout", dirs.out / "arm.stderr"
    started = time.monotonic()
    stop = threading.Event()
    passes: list[float] = []

    def _sampler() -> None:
        while not stop.wait(SAMPLE_SECS):
            at = time.monotonic() - started
            if sample():
                passes.append(at)  # the sample began at `at`, so the pass holds from then
                return

    killed = False
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        proc = subprocess.Popen(
            argv,
            cwd=dirs.repo,
            env=env,
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        sampler = threading.Thread(target=_sampler, name="eval-arm-sampler", daemon=True)
        sampler.start()
        try:
            try:
                proc.wait(timeout=cap_s + KILL_GRACE_SECS)
            except subprocess.TimeoutExpired:
                killed = True
                stop_process(proc)
        except BaseException:
            # Its own session, so a Ctrl-C at the terminal never reaches it, and the
            # caller has no ArmRun to clean up from: stop its whole group here.
            stop.set()
            stop_process(proc)
            kill_group(proc.pid)
            raise
        wall = time.monotonic() - started
        stop.set()
    sampler.join(CHECK_TIMEOUT_SECS)
    stdout = out_path.read_text(encoding="utf-8", errors="replace")
    return ArmRun(
        proc.returncode if proc.returncode is not None else -9,
        stdout,
        wall,
        killed,
        passes[0] if passes else None,
        proc.pid,
    )


def score(
    outcome: ArmOutcome,
    check: dict[str, Any],
    wall_s: float,
    caps: Caps,
    audit: str,
) -> tuple[str, str]:
    """(verdict, reason) for one run, by the rules of §5, in their order.

    The spend cap is read twice: the arm's own stop (``budget``) and the final cost it
    reports, since a turn that settles after the arm's last poll can still pass the cap.
    """
    if outcome.status == "void":
        return "VOID", outcome.note or "harness could not run the task"
    if audit:
        return "VOID", audit
    if outcome.asks >= 1:
        return "FAILED", "ask"
    if outcome.status == "timeout" or wall_s > caps.timeout_s:
        return "FAILED", "timeout"
    cost_cap = {"credits": caps.credits, "usd": caps.usd}.get(outcome.cost_unit)
    over = outcome.cost is not None and cost_cap is not None and outcome.cost > cost_cap
    if outcome.status == "budget" or over:
        return "FAILED", "spend_cap"
    if outcome.status != "done":
        return "FAILED", outcome.status
    if not check.get("pass"):
        return "FAILED", "hidden_check"
    return "PASSED", ""


def audit_run(dirs: RunDirs, suite_dir: Path) -> str:
    """Why the run is void because its output names the case suite, or "" (§7.1).

    The suite path counts only as a whole path component, so a sibling such as
    ``evals/tasks-runs`` (an eval home next to the suite) is not a hit.
    """
    import re

    needle = re.compile(re.escape(str(suite_dir.resolve())) + r"(?![\w.-])")
    for path in dirs.out.rglob("*"):
        if path.is_file() and needle.search(path.read_text(encoding="utf-8", errors="replace")):
            return f"audit: {path.name} names the case suite path"
    return ""


def run_one(
    case: Case,
    arm: Arm,
    index: int,
    eval_root: Path,
    out_dir: Path,
    suite_dir: Path,
    model: str,
    source_env: dict[str, str],
) -> dict[str, Any]:
    """One run of *case* on *arm*; writes and returns its result record."""
    run_id = f"{case.id}-{arm.name}-{index}-{uuid.uuid4().hex[:8]}"
    record: dict[str, Any] = {
        "case": case.id,
        "case_version": case.version,
        "tier": case.tier,
        "arm": arm.name,
        "repeat": index,
        "run_id": run_id,
        "model": model,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    reason = arm.unavailable()
    if reason:
        record.update(verdict="VOID", reason=reason, asks=0, ask_events=[], wall_s=0.0)
        record.update(
            cost=None, cost_unit=arm.cost_unit, idle_after_done_s=None, check=None, agent=None
        )
        return _write_record(out_dir, record)
    dirs = RunDirs(eval_root / run_id)
    caps = case.caps()
    env = run_env(dirs, arm, source_env)
    gateway: subprocess.Popen[bytes] | None = None
    ran: ArmRun | None = None
    try:
        build_repo(case, dirs.repo, dirs.origin)
        if arm.needs_gateway:
            write_crew_config(dirs)
            gateway, port = start_gateway(dirs, env)
            env["KIROCREW_PORT"] = str(port)
        seed = case.seed.replace("{repo}", str(dirs.repo))
        argv = arm.argv(seed, str(dirs.repo), caps, model, case.tools)
        ran = drive_arm(
            argv, dirs, env, caps.timeout_s, lambda: bool(run_check(case, dirs.origin)["pass"])
        )
        # Judged while the gateway (and any server the agent started) is still up.
        check = run_check(case, dirs.origin)
        outcome = arm.parse(ran.returncode, ran.stdout)
        if ran.killed:
            outcome.status = "timeout"
        if arm.needs_gateway:
            _copy_transcript(ran.stdout, dirs)
        verdict, why = score(outcome, check, ran.wall_s, caps, audit_run(dirs, suite_dir))
        if ran.first_pass_s is not None:
            first_pass = ran.first_pass_s
        elif check["pass"]:
            first_pass = ran.wall_s
        else:
            first_pass = None
        record.update(
            verdict=verdict,
            reason=why,
            arm_status=outcome.status,
            arm_note=outcome.note,
            exit_code=ran.returncode,
            asks=outcome.asks,
            ask_events=outcome.ask_events,
            wall_s=round(ran.wall_s, 1),
            cost=outcome.cost,
            cost_unit=outcome.cost_unit,
            idle_after_done_s=(
                None if first_pass is None else round(max(0.0, ran.wall_s - first_pass), 1)
            ),
            check=check,
            agent=outcome.agent,
        )
    except (OSError, RuntimeError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        record.update(
            verdict="VOID", reason=f"harness: {exc}"[:500], asks=0, ask_events=[], wall_s=0.0
        )
        record.update(
            cost=None, cost_unit=arm.cost_unit, idle_after_done_s=None, check=None, agent=None
        )
    finally:
        # After the final check, on every path: what the agent left running dies too.
        if ran is not None:
            kill_group(ran.pgid)
        if gateway is not None:
            stop_process(gateway)
        try:
            _copy_out(dirs, out_dir / "runs" / run_id)
        finally:
            shutil.rmtree(dirs.root, ignore_errors=True)
    return _write_record(out_dir, record)


def _copy_transcript(stdout: str, dirs: RunDirs) -> None:
    """Copy a Crew run's transcript (inside the run's data home) next to its output."""
    from kiro_crew.eval.arms import _last_json_object

    payload = _last_json_object(stdout) or {}
    path = Path(str(payload.get("transcript") or ""))
    if path.is_file() and dirs.crew.resolve() in path.resolve().parents:
        # Copied to a fresh name, then swapped in: a link the agent left at the
        # destination is replaced, never followed.
        tmp = dirs.out / f".transcript.{uuid.uuid4().hex}"
        shutil.copyfile(path, tmp)
        os.replace(tmp, dirs.out / "transcript.jsonl")


def _copy_out(dirs: RunDirs, dest: Path) -> None:
    """Copy the run's output; links are copied as links, never followed.

    The agent can write into the run directory, so a link it leaves (dangling, or
    to a file elsewhere) must neither fail the batch nor pull that file out.
    """
    if not dirs.out.is_dir():
        return
    try:
        shutil.copytree(
            dirs.out,
            dest,
            symlinks=True,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("result.json"),  # the runner's own file
        )
    except (shutil.Error, OSError):
        pass  # what could be copied is kept; result.json is written separately


def _write_record(out_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Publish the run's ``result.json`` by atomic replacement.

    A link already at that name (the agent can write the run output that is copied
    next to it) is replaced as a link, never followed, so no other file is written.
    """
    run_dir = out_dir / "runs" / record["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp = run_dir / f".result.json.{uuid.uuid4().hex}"
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, run_dir / "result.json")
    return record


def _base_env() -> dict[str, str]:
    """The environment for the suite's own build, check and reference git work.

    Git's auto-maintenance is off: after a commit or push, git starts a DETACHED
    ``git maintenance run --auto``, which holds ``objects/maintenance.lock`` while a
    local clone of that repository copies ``objects/``; the lock is removed mid-copy
    and the clone dies with ENOENT. These repositories are throwaway, so nothing is lost.
    """
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ") if os.environ.get(k)}
    env.update(
        GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="maintenance.auto", GIT_CONFIG_VALUE_0="false"
    )
    return env


# ---------- the batch ----------
def summarize(records: list[dict[str, Any]], ks: dict[str, int]) -> list[dict[str, Any]]:
    """One row per (arm, tier): counts, pass^k over cases, and medians of scored runs.

    *ks* is each case's requested number of runs: a case counts toward pass^k only when
    all of them happened and PASSED, so a VOID run or a batch that stopped early leaves
    it incomplete.
    """
    rows = []
    for arm, tier in sorted({(r["arm"], r["tier"]) for r in records}):
        mine = [r for r in records if r["arm"] == arm and r["tier"] == tier]
        scored = [r for r in mine if r["verdict"] != "VOID"]
        # Every requested run counts toward pass^k: a VOID run leaves its case incomplete.
        by_case: dict[str, list[str]] = {}
        for r in mine:
            by_case.setdefault(r["case"], []).append(r["verdict"])
        rows.append(
            {
                "arm": arm,
                "tier": tier,
                "runs": len(mine),
                "passed": sum(r["verdict"] == "PASSED" for r in mine),
                "failed": sum(r["verdict"] == "FAILED" for r in mine),
                "void": len(mine) - len(scored),
                "runs_with_asks": sum(r["asks"] >= 1 for r in scored),
                "cases": len(by_case),
                "cases_pass_k": sum(
                    len(vs) == ks.get(case, 0) and all(v == "PASSED" for v in vs)
                    for case, vs in by_case.items()
                ),
                "median_wall_s": _median([r["wall_s"] for r in scored]),
                "median_cost": _median([r["cost"] for r in scored]),
                "cost_unit": next((r["cost_unit"] for r in mine if r.get("cost_unit")), ""),
                "median_idle_after_done_s": _median([r["idle_after_done_s"] for r in scored]),
            }
        )
    return rows


def _median(values: list[Any]) -> float | None:
    nums = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(nums), 2) if nums else None


def format_summary(rows: list[dict[str, Any]]) -> str:
    """The summary rows as a Markdown table."""
    head = "| arm | tier | runs | passed | failed | void | asked | pass^k cases | wall s | cost | idle s |"
    lines = ["# kirocrew eval run: tasks", "", head, "|" + "---|" * 11]
    for r in rows:
        cost = "" if r["median_cost"] is None else f"{r['median_cost']} {r['cost_unit']}"
        lines.append(
            f"| {r['arm']} | {r['tier']} | {r['runs']} | {r['passed']} | {r['failed']} | {r['void']} "
            f"| {r['runs_with_asks']} | {r['cases_pass_k']}/{r['cases']} | {_cell(r['median_wall_s'])} "
            f"| {cost} | {_cell(r['median_idle_after_done_s'])} |"
        )
    return "\n".join(lines)


def _cell(value: float | None) -> str:
    return "" if value is None else f"{value:g}"


def run_batch(
    cases: list[Case],
    arms: list[Arm],
    repeat: int | None,
    eval_root: Path,
    out_dir: Path,
    suite_dir: Path,
    model: str,
    source_env: dict[str, str],
) -> list[dict[str, Any]]:
    """Every run of the batch, repeat by repeat, so a slow arm does not bunch its runs.

    The suite (cases, hidden checks, ``lib/``) is fingerprinted first and after every
    run. A run that changed it is VOID, and the batch stops there: the agent and the
    hidden check run as the eval user, so a run could otherwise rewrite the grader
    every later run is judged by.
    """
    records: list[dict[str, Any]] = []
    expected = suite_digest(suite_dir)
    ks = requested_runs(cases, repeat)
    for index in range(1, max(ks.values(), default=0) + 1):
        for case in cases:
            if index > ks[case.id]:
                continue
            for arm in arms:
                record = run_one(case, arm, index, eval_root, out_dir, suite_dir, model, source_env)
                records.append(record)
                if suite_digest(suite_dir) != expected:
                    record.update(
                        verdict="VOID", reason="audit: the case suite changed during this run"
                    )
                    _write_record(out_dir, record)
                    print("Stopped: the case suite changed during a run.", file=sys.stderr)
                    return records
    return records


def requested_runs(cases: list[Case], repeat: int | None) -> dict[str, int]:
    """Runs per case: ``--repeat`` when given, else the case's tier k (§3.1)."""
    return {c.id: repeat or int(TIERS[c.tier]["k"]) for c in cases}


def suite_digest(suite_dir: Path) -> str:
    """A fingerprint of every file in the suite, bytecode caches included.

    The checks run with ``-B`` and a private pycache prefix, so no cache appears
    here on its own; one that does was planted, and changes the fingerprint.
    """
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(suite_dir.rglob("*")):
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(suite_dir)).encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def resolve_harnesses(value: str) -> list[Arm]:
    """``all`` or a comma list of arm names; raises ValueError on an unknown one."""
    names = list(ARMS) if value == "all" else [n.strip() for n in value.split(",") if n.strip()]
    unknown = [n for n in names if n not in ARMS]
    if unknown or not names:
        raise ValueError(
            f"unknown harness {', '.join(unknown) or value!r}; choose from {', '.join(ARMS)} or all"
        )
    return [ARMS[n] for n in dict.fromkeys(names)]


def resolve_suite(value: str) -> Path:
    """``tasks`` (``evals/tasks`` under the current directory) or a suite directory."""
    path = Path("evals/tasks") if value == "tasks" else Path(value).expanduser()
    return path.resolve()
