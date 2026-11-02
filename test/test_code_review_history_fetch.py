"""Code Review's jobs read the same git data from a two-commit checkout as from a full clone.

Every job in `code-review.yml` checks out the merge commit at `fetch-depth: 2`
and then runs `.github/scripts/code-review-history.sh` as its "Resolve diff base
(merge-base)" step. A full-history checkout downloads every branch with every
blob, and on a slow runner that fetch outlasted the jobs' timeout.

The invariant: once that step has run, every git read the jobs make (merge-base,
commit range, the rule diffs, the base tip's scripts, Semgrep's baseline) gives
the answer a full clone gives, with no further fetch. These cases build a local
origin with GitHub-style `refs/pull/<n>/merge` refs, check out like
`actions/checkout`, run the step's real `run:` body, then point `origin` at
nothing and compare every read with a full clone.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from git_maintenance_helpers import git_global_config_text, git_maintenance_off_entries

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "code-review.yml"
SCRIPT = ROOT / ".github" / "scripts" / "code-review-history.sh"
JOBS = ("autosde-rules", "inclusive-language", "sast", "pr-hygiene")
DIFF_STEP = "Resolve diff base (merge-base)"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None or shutil.which("git") is None,
    reason="runs the workflow's bash steps against real git; Linux/macOS runners cover it",
)


def _steps(job: str) -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"][job]["steps"]


def _checkout(job: str) -> dict:
    (checkout,) = [s for s in _steps(job) if str(s.get("uses", "")).startswith("actions/checkout@")]
    return checkout.get("with") or {}


def test_every_job_is_covered() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    assert set(jobs) == set(JOBS), sorted(jobs)


@pytest.mark.parametrize("job", JOBS)
def test_the_checkout_is_two_commits_and_the_history_step_precedes_every_git_read(job: str) -> None:
    assert _checkout(job) == {"fetch-depth": 2, "persist-credentials": False}
    steps = _steps(job)
    names = [s.get("name") for s in steps]
    step = steps[names.index(DIFF_STEP)]
    assert step["id"] == "diffbase"
    assert step["run"].strip() == "bash .github/scripts/code-review-history.sh"
    assert step["env"] == {
        "BASE_TIP": "${{ github.event.pull_request.base.sha }}",
        "HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
    }
    # Semgrep's --baseline-commit reads git too.
    reads_git = re.compile(r"\bgit\s|--baseline-commit")
    first_git = min(
        i
        for i, s in enumerate(steps)
        if s is not step and reads_git.search(str(s.get("run") or ""))
    )
    assert names.index(DIFF_STEP) < first_git, names


@pytest.mark.parametrize("job", JOBS)
def test_the_workflow_and_the_job_set_no_token(job: str) -> None:
    """The history script runs from the pull request's own commit, so the jobs hand it no token.

    The workflow's own env and the whole job are read, the job's env and every
    step included, since a token set at any of those levels reaches the script.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for scope, value in (("workflow env", workflow.get("env") or {}), (job, workflow["jobs"][job])):
        text = yaml.safe_dump(value)
        for token in ("github.token", "secrets.", "GH_TOKEN", "GITHUB_TOKEN"):
            assert token not in text, (scope, token)


class _Origin:
    """A local origin holding main, PR heads and GitHub-style pull refs."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.path = tmp / "origin"
        gitconfig = tmp / "gitconfig"
        gitconfig.write_text(git_global_config_text(), encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(
            HOME=str(tmp),
            GIT_CONFIG_GLOBAL=str(gitconfig),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="t",
            GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@example.invalid",
        )
        # Stripping GIT_* drops the suite's maintenance entries and global file.
        self.env.update(git_maintenance_off_entries(self.env))
        self.git("init", "-q", "-b", "main", str(self.path), cwd=tmp)
        for key in ("uploadpack.allowFilter", "uploadpack.allowAnySHA1InWant"):
            self.git("config", key, "true")
        self.git("config", "commit.gpgsign", "false")
        self._n = 0
        self.clones = 0

    def git(self, *args: str, cwd: Path | None = None, env: dict | None = None) -> str:
        return subprocess.run(
            ["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args],
            cwd=cwd or self.path,
            env={**self.env, **(env or {})},
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout

    def commit(self, files: dict[str, str | None]) -> str:
        for rel, body in files.items():
            if body is None:
                self.git("rm", "-q", rel)
                continue
            (self.path / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.path / rel).write_text(body, encoding="utf-8")
            self.git("add", rel)
        self._n += 1
        self.git("commit", "-q", "-m", f"c{self._n}")
        return self.git("rev-parse", "HEAD").strip()

    def branch(self, name: str, at: str) -> None:
        self.git("checkout", "-q", "-B", name, at)

    def pull(self, pr: int, base_tip: str, head: str) -> str:
        """`refs/pull/<pr>/merge` with parents (base tip, head), as GitHub mints it."""
        self.git("checkout", "-q", "--detach", base_tip)
        self.git("merge", "-q", "--no-ff", "-m", f"Merge {head} into {base_tip}", head)
        merge = self.git("rev-parse", "HEAD").strip()
        self.git("update-ref", f"refs/pull/{pr}/merge", merge)
        self.git("update-ref", f"refs/pull/{pr}/head", head)
        return merge


def _world(tmp: Path) -> tuple[_Origin, list[dict]]:
    o = _Origin(tmp)
    lines = "".join(f"line {i}\n" for i in range(1, 9))
    o.commit(
        {
            ".github/scripts/code-review-history.sh": SCRIPT.read_text(encoding="utf-8"),
            "src/shared.py": lines,
            "src/old.py": "old = 1\n",
            "website/src/App.tsx": "// app\n",
            "README.md": "r\n",
        }
    )
    fork_point = o.commit({"src/base_only.py": "b = 1\n"})
    cases: list[dict] = []

    o.branch("normal", fork_point)
    normal = o.commit({"website/src/App.tsx": "// app\nel.innerHTML = html\n"})
    cases.append({"name": "normal", "base_tip": fork_point, "parent": fork_point, "head": normal})

    o.branch("multi", fork_point)
    o.commit({"src/new.py": "n = 1\n"})
    o.git("mv", "src/old.py", "src/renamed.py")
    o.commit({"src/base_only.py": None})
    multi = o.commit({"src/shared.py": lines.replace("line 8", "line eight")})
    cases.append(
        {"name": "three-commits", "base_tip": fork_point, "parent": fork_point, "head": multi}
    )

    # main moves after the branch point, in the file the PRs also change.
    o.branch("main", fork_point)
    for i in range(30):
        o.commit({f"docs/d{i}.md": f"{i}\n"})
    moved = o.commit({"src/shared.py": lines.replace("line 1", "line one")})
    o.branch("behind", fork_point)
    behind = o.commit({"src/shared.py": lines.replace("line 7", "line seven")})
    cases.append({"name": "base-moved", "base_tip": moved, "parent": moved, "head": behind})
    # The event can carry a base.sha older than the merge ref's first parent.
    cases.append(
        {"name": "stale-base-sha", "base_tip": fork_point, "parent": moved, "head": behind}
    )

    # A fork head is reachable only through refs/pull/<n>/head.
    o.git("checkout", "-q", "--detach", moved)
    fork_head = o.commit({"src/fork.py": "f = 1\n"})
    cases.append({"name": "fork", "base_tip": moved, "parent": moved, "head": fork_head})

    for pr, case in enumerate(cases, start=1):
        case["pr"] = pr
        case["merge"] = o.pull(pr, case["parent"], case["head"])
    o.git("checkout", "-q", "main")
    for name in ("normal", "multi", "behind"):
        o.git("branch", "-q", "-D", name)
    return o, cases


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> tuple[_Origin, list[dict], Path]:
    tmp = tmp_path_factory.mktemp("code-review-history")
    o, cases = _world(tmp)
    full = tmp / "full"
    o.git("clone", "-q", "--no-local", str(o.path), str(full), cwd=tmp)
    o.git("fetch", "-q", "origin", "+refs/pull/*:refs/pull/*", cwd=full)
    return o, cases, full


def _checked_out(o: _Origin, case: dict, job: str) -> Path:
    """What actions/checkout leaves for this job: the merge commit, no credentials."""
    o.clones += 1
    work = o.tmp / f"{job}-{case['name']}-{o.clones}"
    o.git("init", "-q", str(work), cwd=o.tmp)
    o.git("remote", "add", "origin", o.path.as_uri(), cwd=work)
    depth = _checkout(job)["fetch-depth"]
    o.git("fetch", "-q", "--no-tags", f"--depth={depth}", "origin", case["merge"], cwd=work)
    o.git("checkout", "-q", "--force", "--detach", case["merge"], cwd=work)
    return work


def _run(o: _Origin, work: Path, script: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=work,
        env={**o.env, **env},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _diff_base(
    o: _Origin, case: dict, job: str, script: str | None = None
) -> tuple[Path, subprocess.CompletedProcess, str]:
    work = _checked_out(o, case, job)
    out = o.tmp / f"out-{work.name}"
    out.write_text("", encoding="utf-8")
    step = next(s for s in _steps(job) if s.get("name") == DIFF_STEP)
    env = {
        "BASE_TIP": case["base_tip"],
        "HEAD_SHA": case["head"],
        "GITHUB_OUTPUT": str(out),
    }
    res = _run(o, work, script or step["run"], env)
    sha = dict(
        line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line
    ).get("sha", "")
    return work, res, sha


#: Every git read the four jobs make after the diff-base step; MB is the merge-base.
READS = (
    ("diff", "--name-only", "MB..HEAD"),
    ("diff", "MB..HEAD", "--", "src/**/*.py"),
    ("diff", "MB..HEAD", "--", ":(glob)src/**/*.py", ":(exclude)src/old.py"),
    ("-C", "website", "diff", "MB..HEAD", "--", ":(glob)src/**/*.tsx", ":(glob)src/**/*.ts"),
    ("diff", "MB..HEAD", "--unified=0", "--diff-filter=ACMR", "--", ":(exclude)README.md"),
    ("diff", "MB", "HEAD"),
    ("diff", "--name-only", "--no-renames", "MB", "HEAD"),
    ("rev-list", "--count", "BASE..HEAD"),
    ("ls-tree", "--name-only", "BASE", "--", ".github/scripts/code-review-history.sh"),
    ("ls-tree", "--name-only", "BASE", "--", ".github/scripts/absent_check.py"),
    ("show", "BASE:.github/scripts/code-review-history.sh"),
    ("diff", "--binary", "MB", "MERGE"),
    ("diff", "--name-status", "MB", "MERGE"),
)


@pytest.mark.parametrize("job", JOBS)
def test_every_read_matches_a_full_clone_with_no_further_fetch(world, job: str) -> None:
    o, cases, full = world
    for case in cases:
        work, res, sha = _diff_base(o, case, job)
        assert res.returncode == 0, f"{case['name']}\n{res.stdout}\n{res.stderr}"
        expected_mb = o.git("merge-base", case["base_tip"], case["head"]).strip()
        assert sha == expected_mb, case["name"]
        # Nothing to fetch from any more: a read that needed a fetch would fail.
        o.git("remote", "set-url", "origin", (o.tmp / "gone").as_uri(), cwd=work)
        names = {"MB": sha, "HEAD": case["head"], "BASE": case["base_tip"], "MERGE": case["merge"]}
        for read in READS:
            args = [a.replace("MB", names["MB"]).replace("HEAD", names["HEAD"]) for a in read]
            args = [a.replace("BASE", names["BASE"]).replace("MERGE", names["MERGE"]) for a in args]
            got = o.git(*args, cwd=work, env={"GIT_NO_LAZY_FETCH": "1"})
            assert got == o.git(*args, cwd=full), (case["name"], read)


def test_the_commit_range_and_the_rule_greps_run_on_the_fetched_history(world) -> None:
    o, cases, _ = world
    by_name = {c["name"]: c for c in cases}
    steps = {s.get("name"): s for s in _steps("pr-hygiene") + _steps("autosde-rules")}
    for name, count in (("normal", 1), ("three-commits", 3), ("base-moved", 1), ("fork", 1)):
        work, res, _ = _diff_base(o, by_name[name], "pr-hygiene")
        assert res.returncode == 0, res.stderr
        env = {
            "BASE": by_name[name]["base_tip"],
            "HEAD": by_name[name]["head"],
            "BASE_REF": "main",
            "MAX_COMMITS": "2",
        }
        enforced = _run(
            o, work, steps["Enforce commit count"]["run"], {**env, "GIT_NO_LAZY_FETCH": "1"}
        )
        assert (enforced.returncode == 0) is (count <= 2), (name, enforced.stdout)
        assert (
            f"({count} of at most" in enforced.stdout or f"(has {count})" in enforced.stdout
        ), enforced.stdout
    work, res, sha = _diff_base(o, by_name["normal"], "autosde-rules")
    assert res.returncode == 0, res.stderr
    rules = _run(
        o,
        work / "website",
        steps["Check frontend blocking rules"]["run"],
        {"BASE": sha, "HEAD": by_name["normal"]["head"], "GIT_NO_LAZY_FETCH": "1"},
    )
    assert rules.returncode != 0 and "Never write HTML via innerHTML" in rules.stdout, rules.stdout


def test_a_fetch_that_fails_once_is_retried(world) -> None:
    o, cases, _ = world
    real_git = shutil.which("git")
    shim = o.tmp / f"shim-{o.clones}"
    shim.mkdir()
    failed = shim / "failed-once"
    (shim / "git").write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "$1" = fetch ] && [ ! -e "{failed}" ]; then\n'
        f'  : > "{failed}"; echo "fatal: transient failure" >&2; exit 128\n'
        "fi\n"
        f'exec "{real_git}" "$@"\n',
        encoding="utf-8",
    )
    (shim / "git").chmod(0o755)
    stale = next(c for c in cases if c["name"] == "stale-base-sha")
    script = f'PATH="{shim}{os.pathsep}$PATH" bash .github/scripts/code-review-history.sh'
    _, res, sha = _diff_base(o, stale, "pr-hygiene", script)
    assert failed.exists(), "the first fetch never ran through the shim"
    assert res.returncode == 0, res.stdout + res.stderr
    assert sha == o.git("merge-base", stale["base_tip"], stale["head"]).strip()


def _fetch_shim(o: _Origin, name: str, on_first_fetch: str) -> tuple[Path, Path]:
    """A ``git`` that runs *on_first_fetch* for the first fetch and logs every fetch's argv.

    ``REAL`` in *on_first_fetch* is the real git. The shim also holds a ``sleep``
    that returns at once, so the script's retry backoff costs no time here.
    """
    real_git = shutil.which("git")
    shim = o.tmp / f"{name}-{o.clones}"
    shim.mkdir()
    first = shim / "first-fetch"
    log = shim / "fetches"
    (shim / "git").write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "$1" = fetch ]; then printf "%s\\n" "$*" >> "{log}"; fi\n'
        f'if [ "$1" = fetch ] && [ ! -e "{first}" ]; then\n'
        f'  : > "{first}"\n'
        f"  {on_first_fetch.replace('REAL', real_git)}\n"
        "fi\n"
        f'exec "{real_git}" "$@"\n',
        encoding="utf-8",
    )
    (shim / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for tool in ("git", "sleep"):
        (shim / tool).chmod(0o755)
    return shim, log


def test_retry_after_a_fetch_that_unshallowed_then_errored(world) -> None:
    """An attempt that completed the history and then failed is retried without --unshallow.

    git refuses --unshallow on a complete history, so asking for it again would
    fail every retry on a transient error.
    """
    o, cases, _ = world
    shim, log = _fetch_shim(
        o,
        "unshallow-then-error",
        '"REAL" "$@"; echo "error: failed after the pack landed" >&2; exit 1',
    )
    stale = next(c for c in cases if c["name"] == "stale-base-sha")
    script = f'PATH="{shim}{os.pathsep}$PATH" bash .github/scripts/code-review-history.sh'
    _, res, sha = _diff_base(o, stale, "pr-hygiene", script)
    assert res.returncode == 0, res.stdout + res.stderr
    assert sha == o.git("merge-base", stale["base_tip"], stale["head"]).strip()
    first, retry, *_ = log.read_text(encoding="utf-8").splitlines()
    assert "--unshallow" in first.split() and "--unshallow" not in retry.split(), (first, retry)


def test_a_fetch_that_leaves_the_history_shallow_fails_the_step(world) -> None:
    """A fetch that reports success without completing the history is not taken on trust."""
    o, cases, _ = world
    shim, _ = _fetch_shim(o, "shallow-success", "exit 0")
    stale = next(c for c in cases if c["name"] == "stale-base-sha")
    script = f'PATH="{shim}{os.pathsep}$PATH" bash .github/scripts/code-review-history.sh'
    _, res, sha = _diff_base(o, stale, "pr-hygiene", script)
    assert res.returncode != 0 and "is still shallow after the fetch" in res.stdout, (
        res.stdout + res.stderr
    )
    assert sha == ""


def test_a_refused_history_fetch_fails_the_step_and_names_the_anonymous_fetch(world) -> None:
    """The step fails when every fetch is refused, as a private repository refuses it.

    The step clears every credential the runner has, so its error says the
    repository must be public or readable without them.
    """
    o, cases, _ = world
    shim, log = _fetch_shim(o, "refused", ":")
    stale = next(c for c in cases if c["name"] == "stale-base-sha")
    gone = (o.tmp / "gone").as_uri()
    script = (
        f'git remote set-url origin "{gone}" && '
        f'PATH="{shim}{os.pathsep}$PATH" bash .github/scripts/code-review-history.sh'
    )
    _, res, sha = _diff_base(o, stale, "pr-hygiene", script)
    assert res.returncode != 0, res.stdout + res.stderr
    says = (
        f"::error::Could not fetch the history of {stale['base_tip']} and {stale['head']}."
        " The history is fetched without credentials, so the repository must be public"
        " or readable without them."
    )
    assert says in res.stdout.splitlines(), res.stdout + res.stderr
    assert len(log.read_text(encoding="utf-8").splitlines()) == 3
    assert sha == ""


@pytest.mark.parametrize(
    ("cut", "until", "says"),
    [
        # Without the history fetch the checkout's history stays shallow.
        ("if ! retry fetch_history", "\nfi\n", "is still shallow after the fetch"),
        # Without the diffs the merge-base's files are missing, and the check says so.
        ('for tip in HEAD "$HEAD_SHA" "$BASE_TIP"; do', "\ndone\n", "are not local"),
    ],
    ids=["no-history-fetch", "no-merge-base-files"],
)
def test_the_step_fails_when_a_fetch_it_needs_is_removed(
    world, cut: str, until: str, says: str
) -> None:
    o, cases, _ = world
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index(cut)
    end = text.index(until, start) + len(until)
    mutated = o.tmp / f"mutated-{o.clones}.sh"
    mutated.write_text(text[:start] + text[end:], encoding="utf-8")
    stale = next(c for c in cases if c["name"] == "stale-base-sha")
    _, res, sha = _diff_base(o, stale, "pr-hygiene", f"bash {mutated}")
    assert res.returncode != 0 and says in res.stdout, res.stdout + res.stderr
    assert sha == ""


def _trace_processes(trace_dir: Path) -> list[tuple[list[str], dict[str, list[str]]]]:
    """Each traced git process: its argv, and the values it read for each traced key, in order."""
    processes = []
    for path in sorted(trace_dir.iterdir()):
        events = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        ]
        argv = next((e["argv"] for e in events if e.get("event") == "start"), [])
        params: dict[str, list[str]] = {}
        for e in events:
            if e.get("event") == "def_param":
                params.setdefault(e["param"], []).append(e["value"])
        processes.append((argv, params))
    return processes


def test_no_fetch_has_a_credential_or_header_to_send_even_when_the_runner_has_them(world) -> None:
    """Every fetch the step starts, the on-demand ones during the diffs included, is anonymous.

    The runner's git config hands out a credential and sets the
    ``http.https://github.com/.extraheader`` that ``actions/checkout`` writes.
    The script's own fetch is checked through a ``git`` shim. The on-demand
    fetches start from git's exec-path, which no shim on PATH sees, so the git
    trace records what each fetch read. The merge-base here is older than both
    of the merge commit's parents, so its files are fetched on demand.
    """
    o, cases, _ = world
    real_git = shutil.which("git")
    shim = o.tmp / f"cred-shim-{o.clones}"
    shim.mkdir()
    seen = shim / "seen"
    trace = shim / "trace2"
    trace.mkdir()
    header = "AUTHORIZATION: basic eC1hY2Nlc3MtdG9rZW46ZHVtbXk="
    runner_config = shim / "gitconfig"
    runner_config.write_text(
        git_global_config_text()
        + f'[http "https://github.com/"]\n\textraheader = {header}\n'
        + '[credential]\n\thelper = "!f() { echo username=x; echo password=leaked; }; f"\n',
        encoding="utf-8",
    )
    ask = "protocol=https\nhost=github.com\n\n"
    url = "https://github.com/kirodotdev/KiroCrew"
    runner_env = {**o.env, "GIT_CONFIG_GLOBAL": str(runner_config)}
    # Outside the script, the runner's config hands out both.
    outside = subprocess.run(
        [real_git, "credential", "fill"],
        input=ask,
        env=runner_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert "password=leaked" in outside.stdout, outside.stdout + outside.stderr
    sent = o.git("config", "--get-urlmatch", "http.extraheader", url, env=runner_env)
    assert sent.strip() == header
    # Inside the script, the script's own fetch records what it could send.
    (shim / "git").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = fetch ]; then\n'
        f'  ( echo "header=[$("{real_git}" config --get-urlmatch http.extraheader {url})]"\n'
        f'    printf "protocol=https\\nhost=github.com\\n\\n" | "{real_git}" credential fill'
        f' ) >> "{seen}" 2>&1\n'
        "fi\n"
        f'exec "{real_git}" "$@"\n',
        encoding="utf-8",
    )
    (shim / "git").chmod(0o755)
    moved = next(c for c in cases if c["name"] == "base-moved")
    script = (
        f'GIT_CONFIG_GLOBAL="{runner_config}" PATH="{shim}{os.pathsep}$PATH" '
        f'GIT_TRACE2_EVENT="{trace}" GIT_TRACE2_ENV_VARS=GIT_TERMINAL_PROMPT '
        'GIT_TRACE2_CONFIG_PARAMS="credential.helper,http.*extraheader" '
        "bash .github/scripts/code-review-history.sh"
    )
    _, res, sha = _diff_base(o, moved, "sast", script)
    assert res.returncode == 0, res.stdout + res.stderr
    assert sha == o.git("merge-base", moved["base_tip"], moved["head"]).strip()
    assert sha not in o.git("rev-list", "--parents", "-n", "1", moved["merge"]).split()
    record = seen.read_text(encoding="utf-8")
    assert "header=[]" in record and header not in record, record
    assert "password=leaked" not in record and "terminal prompts disabled" in record, record
    fetches = [(argv, params) for argv, params in _trace_processes(trace) if "fetch" in argv]
    on_demand = [argv for argv, _ in fetches if "--stdin" in argv]
    assert on_demand, [argv for argv, _ in fetches]
    for argv, params in fetches:
        assert params.get("credential.helper", [""])[-1] == "", (argv, params)
        assert params["http.https://github.com/.extraheader"][-1] == "", (argv, params)
        assert params["GIT_TERMINAL_PROMPT"] == ["0"], (argv, params)


#: The scripts PR Hygiene's checker steps run from the base tip.
CHECKERS = (".github/scripts/proof_line_check.py", ".github/scripts/sibling_list_check.py")


@pytest.fixture(scope="module")
def checker_world(tmp_path_factory: pytest.TempPathFactory) -> tuple[_Origin, dict[str, dict]]:
    o = _Origin(tmp_path_factory.mktemp("code-review-checkers"))
    o.commit({".github/scripts/code-review-history.sh": SCRIPT.read_text(encoding="utf-8")})
    no_checkers = o.commit({"README.md": "r\n"})
    with_checkers = o.commit({path: 'print("base copy")\n' for path in CHECKERS})
    moved = o.commit({"docs/d.md": "d\n"})
    checkout_copy = {path: 'print("checkout copy")\n' for path in CHECKERS}
    cases = {}
    for pr, (name, base_tip, parent) in enumerate(
        (
            ("base-has-the-checkers", with_checkers, with_checkers),
            ("base-predates-the-checkers", no_checkers, no_checkers),
            # base.sha older than the merge ref's first parent, with no
            # diff-base step: the base tip's objects are not local.
            ("base-tip-not-local", with_checkers, moved),
        ),
        start=1,
    ):
        o.git("checkout", "-q", "--detach", parent)
        head = o.commit(checkout_copy)
        cases[name] = {"name": name, "base_tip": base_tip, "head": head}
        cases[name]["merge"] = o.pull(pr, parent, head)
    o.git("checkout", "-q", "main")
    return o, cases


@pytest.mark.parametrize("checker", CHECKERS)
@pytest.mark.parametrize(
    ("case", "says"),
    [
        ("base-has-the-checkers", "base copy"),
        ("base-predates-the-checkers", "checkout copy"),
        ("base-tip-not-local", None),
    ],
)
def test_a_checker_runs_the_base_tips_copy_and_a_failed_read_fails_the_step(
    checker_world, checker: str, case: str, says: str | None
) -> None:
    o, cases = checker_world
    if says is None:
        work = _checked_out(o, cases[case], "pr-hygiene")
    else:
        work, res, _ = _diff_base(o, cases[case], "pr-hygiene")
        assert res.returncode == 0, res.stdout + res.stderr
    o.git("remote", "set-url", "origin", (o.tmp / "gone").as_uri(), cwd=work)
    (run,) = [s["run"] for s in _steps("pr-hygiene") if checker in str(s.get("run"))]
    # `bash -e`, as the runner runs a step that names no shell; TMPDIR keeps
    # the step's mktemp inside this test's folder.
    got = subprocess.run(
        ["bash", "-e", "-c", run],
        cwd=work,
        env={
            **o.env,
            "BASE_TIP": cases[case]["base_tip"],
            "GIT_NO_LAZY_FETCH": "1",
            "TMPDIR": str(o.tmp),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if says is None:
        assert got.returncode != 0 and "copy" not in got.stdout, got.stdout + got.stderr
    else:
        assert got.returncode == 0 and got.stdout == f"{says}\n", got.stdout + got.stderr
