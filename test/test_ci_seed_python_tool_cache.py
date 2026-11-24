"""The Python tool-cache seed: where it runs, what it pins, and how it fails.

On a CodeBuild runner ``actions/setup-python`` finds no Python in the tool
cache and reads the publisher's manifest through the GitHub API, twice per
step, with the job's token. ``.github/actions/seed-python-tool-cache`` installs
the pinned archive first so that lookup never happens. Three things have to
hold for that to be safe, and each is checked against the tree, not a list:

* every ``setup-python`` step a CodeBuild runner can reach is preceded by the
  seed, asking for the same version, in a job whose checkout holds the action
  -- and in a workflow another repository can call, whose checkout is then
  the caller's tree, the seed runs only in this repository
  -- except in the Fast Gate workflow, whose own contract
  (``test_fast_gate_barrier.py``) puts ``setup-python`` directly after the
  checkout, and whose jobs therefore carry no seed;
* the pinned table has an archive for every version those steps request, from
  the publisher ``setup-python`` downloads from;
* the seed opens an archive only after its SHA-256 matched, fails the job on a
  mismatch, and in every other case leaves the tool cache as it found it and
  lets ``setup-python`` install Python itself.

The Linux step is executed here against a stub ``curl`` and a fixture archive.
The Windows step cannot run on this host, so its text is held to the same
order of operations.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import NamedTuple

import pytest
import test_fast_gate_barrier as barrier
import yaml

from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ACTION_DIR = ROOT / ".github" / "actions" / "seed-python-tool-cache"
ACTION_REF = "./.github/actions/seed-python-tool-cache"
SETUP_PYTHON = "actions/setup-python@"
PUBLISHER_RELEASES = "https://github.com/actions/python-versions/releases/download/"
# The label prefix the fleet's CodeBuild projects answer to, per OS.
CODEBUILD_LABEL = {
    "linux": "codebuild-kirocrew-gha-linux",
    "win32": "codebuild-kirocrew-gha-windows",
}
# The step condition that keeps the seed out of a run whose checkout is another
# repository's tree. In a called workflow `github.repository` is the caller's.
THIS_REPOSITORY_ONLY = "github.repository == 'kirodotdev/KiroCrew'"
WARNING = "::warning title=seed-python-tool-cache::"
ERROR = "::error title=seed-python-tool-cache::"


class Row(NamedTuple):
    os: str
    os_version: str
    arch: str
    version: str
    sha256: str
    url: str


class Site(NamedTuple):
    workflow: str
    job: str
    index: int
    steps: list
    job_body: dict
    callable_workflow: bool


# Each file is parsed once for the module: the workflows are large, and a parse
# per test or per call site costs more than every assertion here together.
@pytest.fixture(scope="module")
def action() -> dict:
    return yaml.safe_load((ACTION_DIR / "action.yml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def seed_steps(action: dict) -> dict[str, dict]:
    return {step["shell"]: step for step in action["runs"]["steps"]}


@pytest.fixture(scope="module")
def table() -> list[Row]:
    lines = (ACTION_DIR / "python-versions.txt").read_text(encoding="utf-8").splitlines()
    return [Row(*line.split()) for line in lines if line.strip() and not line.startswith("#")]


@pytest.fixture(scope="module")
def workflows() -> dict[str, dict]:
    return {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
    }


def _callable(workflow: dict) -> bool:
    """Whether another workflow, possibly in another repository, can call it."""
    # PyYAML reads the bare key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    return "workflow_call" in (triggers if isinstance(triggers, (dict, list)) else [triggers])


def _expected_seed_condition(site: Site) -> str | None:
    """The `if` the seed must carry: the setup step's own, plus the repository
    guard where the checkout can be another repository's tree."""
    own = site.steps[site.index].get("if")
    if not site.callable_workflow:
        return own
    return THIS_REPOSITORY_ONLY if own is None else f"{THIS_REPOSITORY_ONLY} && ({own})"


def _codebuild_oses(job: dict) -> list[str]:
    """The OSes whose CodeBuild project the job's ``runs-on`` can name."""
    runs_on = json.dumps(job.get("runs-on"))
    return [name for name, label in CODEBUILD_LABEL.items() if label in runs_on]


def _contract_bound(site: Site) -> bool:
    """Whether the site is in the workflow the Fast Gate runtime contract covers.

    That contract holds every job of its workflow to "setup-python directly
    follows checkout", which leaves no place for a step between the two.
    """
    return site.workflow == barrier._FAST_GATE.name


@pytest.fixture(scope="module")
def seeded_sites(sites: list[Site]) -> list[Site]:
    return [site for site in sites if not _contract_bound(site)]


@pytest.fixture(scope="module")
def sites(workflows: dict[str, dict]) -> list[Site]:
    """Every setup-python step in a job a CodeBuild runner can pick up."""
    found = []
    for name, workflow in workflows.items():
        for job_name, job in (workflow.get("jobs") or {}).items():
            if not isinstance(job, dict) or not _codebuild_oses(job):
                continue
            steps = job.get("steps") or []
            for index, step in enumerate(steps):
                if str(step.get("uses", "")).startswith(SETUP_PYTHON):
                    found.append(Site(name, job_name, index, steps, job, _callable(workflow)))
    return found


def _requested_versions(site: Site, spec: object) -> list[str]:
    """The literal versions one ``python-version`` value stands for in its job."""
    text = str(spec)
    matrix_key = re.fullmatch(r"\$\{\{\s*matrix\.([\w-]+)\s*\}\}", text)
    if not matrix_key:
        assert "${{" not in text, f"{site.workflow}:{site.job} computes its version: {text}"
        return [text]
    values = site.job_body["strategy"]["matrix"][matrix_key.group(1)]
    return [str(value) for value in values]


def _satisfies(version: str, spec: str) -> bool:
    return version == spec or version.startswith(spec + ".")


# ── where the seed runs ─────────────────────────────────────────────────


def test_some_setup_python_step_can_reach_codebuild_on_each_os(sites: list[Site]) -> None:
    reachable = {name for site in sites for name in _codebuild_oses(site.job_body)}
    assert reachable == set(CODEBUILD_LABEL), "the routing text changed; these tests measure less"


def test_every_codebuild_setup_python_step_follows_the_seed(
    seeded_sites: list[Site], action: dict
) -> None:
    unseeded = []
    assert seeded_sites
    default = action["inputs"]["python-version"]["default"]
    for site in seeded_sites:
        step = site.steps[site.index]
        seed = site.steps[site.index - 1] if site.index else {}
        seeded_version = (seed.get("with") or {}).get("python-version", default)
        if (
            seed.get("uses") != ACTION_REF
            or seed.get("if") != _expected_seed_condition(site)
            or str(seeded_version) != str(step["with"]["python-version"])
            or "continue-on-error" in seed
        ):
            unseeded.append(f"{site.workflow}:{site.job}")
    assert unseeded == []


def test_a_callable_workflow_seeds_only_in_this_repository(seeded_sites: list[Site]) -> None:
    guarded = [site for site in seeded_sites if site.callable_workflow]
    assert guarded, "no seeded workflow is callable; this test measures nothing"
    for site in guarded:
        condition = str(site.steps[site.index - 1].get("if"))
        assert condition.startswith(THIS_REPOSITORY_ONLY), f"{site.workflow}:{site.job}"
    # The guard belongs to callable workflows only: elsewhere the seed keeps
    # exactly the setup step's own condition.
    for site in seeded_sites:
        if not site.callable_workflow:
            seed, step = site.steps[site.index - 1], site.steps[site.index]
            assert seed.get("if") == step.get("if"), f"{site.workflow}:{site.job}"


def test_the_seed_appears_only_directly_before_setup_python(workflows: dict[str, dict]) -> None:
    stray = []
    for name, workflow in workflows.items():
        for job_name, job in (workflow.get("jobs") or {}).items():
            steps = job.get("steps") or [] if isinstance(job, dict) else []
            for index, step in enumerate(steps):
                if step.get("uses") != ACTION_REF:
                    continue
                following = steps[index + 1] if index + 1 < len(steps) else {}
                if not str(following.get("uses", "")).startswith(SETUP_PYTHON):
                    stray.append(f"{name}:{job_name}")
    assert stray == []


def test_the_fast_gate_jobs_keep_their_own_contract_and_carry_no_seed(
    sites: list[Site], workflows: dict[str, dict]
) -> None:
    bound = [site for site in sites if _contract_bound(site)]
    assert bound, "no Fast Gate job reaches CodeBuild; the exception excuses nothing"
    for site in bound:
        barrier.TestFastGatePythonRuntime._assert_runtime(site.job_body)
        assert all(step.get("uses") != ACTION_REF for step in site.steps), site.job
    # The contract covers every job of that workflow, so none may be left out here.
    fast_gate_jobs = set(workflows[barrier._FAST_GATE.name]["jobs"])
    assert {site.job for site in bound} == fast_gate_jobs


def test_every_seeded_job_checks_the_action_out_first(seeded_sites: list[Site]) -> None:
    action_path = "/" + ACTION_DIR.relative_to(ROOT).as_posix() + "/"
    missing = []
    for site in seeded_sites:
        checkouts = [
            step
            for step in site.steps[: site.index]
            if str(step.get("uses", "")).startswith("actions/checkout@") and "if" not in step
        ]
        options = [step.get("with") or {} for step in checkouts]
        whole_tree = any("sparse-checkout" not in option for option in options)
        listed = any(
            action_path in str(option.get("sparse-checkout", "")).split() for option in options
        )
        elsewhere = any(
            option.get("ref") or option.get("repository") or option.get("path")
            for option in options
        )
        if not (whole_tree or listed) or elsewhere:
            missing.append(f"{site.workflow}:{site.job}")
    assert missing == []


# ── what the table pins ─────────────────────────────────────────────────


def test_the_table_rows_are_pinned_publisher_archives(table: list[Row]) -> None:
    rows = table
    assert rows
    keys = [(row.os, row.os_version, row.arch, row.version) for row in rows]
    assert len(set(keys)) == len(keys)
    for row in rows:
        assert row.os in CODEBUILD_LABEL
        assert re.fullmatch(r"[0-9a-f]{64}", row.sha256), row
        assert re.fullmatch(r"\d+\.\d+\.\d+", row.version), row
        release, _, filename = row.url.removeprefix(PUBLISHER_RELEASES).partition("/")
        assert row.url.startswith(PUBLISHER_RELEASES) and "/" not in filename, row
        assert re.fullmatch(re.escape(row.version) + r"-\d+", release), row
        os_part = row.os if row.os_version == "-" else f"{row.os}-{row.os_version}"
        suffix = ".zip" if row.os == "win32" else ".tar.gz"
        assert filename == f"python-{row.version}-{os_part}-{row.arch}{suffix}", row


def test_the_table_holds_every_version_a_seeded_step_requests(
    table: list[Row], seeded_sites: list[Site]
) -> None:
    rows = table
    unpinned = []
    for site in seeded_sites:
        spec = site.steps[site.index]["with"]["python-version"]
        for version in _requested_versions(site, spec):
            for os_name in _codebuild_oses(site.job_body):
                matches = [
                    row for row in rows if row.os == os_name and _satisfies(row.version, version)
                ]
                if not matches:
                    unpinned.append(f"{site.workflow}:{site.job} {os_name} {version}")
    assert unpinned == []


def test_each_os_and_version_line_has_one_row_so_the_install_is_not_a_choice(
    table: list[Row],
) -> None:
    rows = table
    lines = [(row.os, row.os_version, row.arch, row.version.rsplit(".", 1)[0]) for row in rows]
    assert len(set(lines)) == len(lines)


# ── the two steps are one procedure ─────────────────────────────────────


def test_the_steps_split_by_os_on_self_hosted_runners_only(seed_steps: dict[str, dict]) -> None:
    steps = seed_steps
    assert set(steps) == {"bash", "pwsh"}
    assert steps["bash"]["if"] == "runner.environment == 'self-hosted' && runner.os == 'Linux'"
    assert steps["pwsh"]["if"] == "runner.environment == 'self-hosted' && runner.os == 'Windows'"
    assert all("continue-on-error" not in step for step in steps.values())


def test_both_steps_share_their_inputs_and_carry_no_credential(
    seed_steps: dict[str, dict],
) -> None:
    steps = seed_steps
    assert steps["bash"]["env"] == steps["pwsh"]["env"]
    env = steps["bash"]["env"]
    assert int(env["SEED_DOWNLOAD_ATTEMPTS"]) >= 1
    assert int(env["SEED_DOWNLOAD_ATTEMPT_SECONDS"]) >= 1
    for step in steps.values():
        text = json.dumps(step).lower()
        assert not re.search(r"token|secrets\.|authorization|password", text)
        # Expressions reach the script through env only, never by substitution.
        assert "${{" not in step["run"]


def _positions(text: str, markers: list[str]) -> list[int]:
    found = [text.find(marker) for marker in markers]
    assert all(position >= 0 for position in found), dict(zip(markers, found))
    return found


@pytest.mark.parametrize(
    ("shell", "markers"),
    [
        (
            "bash",
            [
                '-f "$dest.complete"',
                "mktemp -d",
                "curl --fail",
                "sha256sum",
                "exit 1",
                "tar -xzf",
                "./setup.sh",
                'rm -rf "$dest" "$dest.complete"',
            ],
        ),
        (
            "pwsh",
            [
                'Test-Path -LiteralPath "$dest.complete"',
                "GetRandomFileName",
                "GetByteArrayAsync",
                "Get-FileHash -Algorithm SHA256",
                "exit 1",
                "Expand-Archive",
                "./setup.ps1",
                "Remove-Item -LiteralPath $dest -Recurse",
            ],
        ),
    ],
)
def test_each_step_verifies_before_it_unpacks_or_runs(
    seed_steps: dict[str, dict], shell: str, markers: list[str]
) -> None:
    run = seed_steps[shell]["run"]
    positions = _positions(run, markers)
    assert positions == sorted(positions)
    # One failing exit, and it is the mismatch: every other stop is exit 0.
    assert len(re.findall(r"\bexit 1\b", run)) == 1
    assert run.count(ERROR) == 1 and run.index(ERROR) < run.index("exit 1")


def test_both_steps_bound_the_download_and_stay_on_https(seed_steps: dict[str, dict]) -> None:
    steps = seed_steps
    bash, pwsh = steps["bash"]["run"], steps["pwsh"]["run"]
    for flag in (
        "--fail",
        "--proto '=https'",
        "--proto-redir '=https'",
        "--tlsv1.2",
        '--max-time "$SEED_DOWNLOAD_ATTEMPT_SECONDS"',
    ):
        assert flag in bash
    assert 'seq 1 "$SEED_DOWNLOAD_ATTEMPTS"' in bash
    assert 'mktemp -d "$RUNNER_TEMP/' in bash
    assert "$url.StartsWith('https://'" in pwsh
    assert "FromSeconds([int]$env:SEED_DOWNLOAD_ATTEMPT_SECONDS)" in pwsh
    assert "[int]$env:SEED_DOWNLOAD_ATTEMPTS" in pwsh
    assert "Join-Path $env:RUNNER_TEMP" in pwsh
    assert "'Tls12, Tls13'" in pwsh


# ── the Linux step, executed ────────────────────────────────────────────

_VERSION = "3.12.99"
# What the publisher's setup.sh does, in its order: copy the tree into the tool
# cache, then (after the slow part) write the marker. FIXTURE_FAIL stops it
# between the two, the way a failed pip upgrade would.
_FIXTURE_SETUP_SH = f"""\
set -e
target="$RUNNER_TOOL_CACHE/Python/{_VERSION}/x64"
mkdir -p "$target"
cp -R ./* "$target"
echo "loader path: $LD_LIBRARY_PATH" > "$target/loader-path.txt"
if [ -n "${{FIXTURE_FAIL:-}}" ]; then
  echo "installer failed halfway" >&2
  exit 3
fi
touch "$RUNNER_TOOL_CACHE/Python/{_VERSION}/x64.complete"
"""

_STUB_CURL = """\
#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_LOG_DIR/curl.log"
[ -z "${STUB_CURL_FAIL:-}" ] || exit 22
out=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output" ]; then out="$2"; fi
  shift
done
cp "$FIXTURE_ARCHIVE" "$out"
"""

_STUB_RECORDER = """\
#!/bin/sh
echo "{name}" >> "$STUB_LOG_DIR/order.log"
exec "{real}" "$@"
"""


def _host_os_version() -> str:
    text = Path("/etc/os-release").read_text(encoding="utf-8")
    match = re.search(r'^VERSION_ID="?([^"\n]*)"?$', text, re.M)
    return match.group(1) if match else ""


class Seed:
    """One run of the Linux step in a private tool cache, temp dir and PATH."""

    def __init__(self, tmp_path: Path, step: dict) -> None:
        self.step = step
        self.tool_cache = tmp_path / "tool-cache"
        self.temp = tmp_path / "runner-temp"
        self.logs = tmp_path / "logs"
        self.bin = tmp_path / "bin"
        for directory in (self.tool_cache, self.temp, self.logs, self.bin):
            directory.mkdir()
        self.archive = tmp_path / "fixture.tar.gz"
        with tarfile.open(self.archive, "w:gz") as tar:
            for name, body in (("setup.sh", _FIXTURE_SETUP_SH), ("bin/python3.12", "fixture\n")):
                data = body.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        self.sha256 = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.url = f"{PUBLISHER_RELEASES}{_VERSION}-1/python-{_VERSION}-linux-x64.tar.gz"
        self.table = tmp_path / "python-versions.txt"
        self.write_table(sha256=self.sha256)
        self._stub("curl", _STUB_CURL)
        for name in ("tar", "sha256sum"):
            real = shutil.which(name)
            assert real
            self._stub(name, _STUB_RECORDER.format(name=name, real=real))
        self.script = tmp_path / "seed.sh"
        self.script.write_text(step["run"], encoding="utf-8")

    def _stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def write_table(self, *, sha256: str, os_version: str | None = None) -> None:
        version = _host_os_version() if os_version is None else os_version
        self.table.write_text(
            f"# a comment line\nlinux {version} x64 {_VERSION} {sha256} {self.url}\n",
            encoding="utf-8",
        )

    @property
    def dest(self) -> Path:
        return self.tool_cache / "Python" / _VERSION / "x64"

    @property
    def marker(self) -> Path:
        return self.dest.with_name("x64.complete")

    def log(self, name: str) -> list[str]:
        path = self.logs / name
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def run(self, **extra: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TOOL_CACHE": str(self.tool_cache),
            "RUNNER_TEMP": str(self.temp),
            "RUNNER_ARCH": "X64",
            "PYTHON_SPEC": "3.12",
            "SEED_TABLE": str(self.table),
            "STUB_LOG_DIR": str(self.logs),
            "FIXTURE_ARCHIVE": str(self.archive),
            **{k: v for k, v in self.step["env"].items() if k.startswith("SEED_DOWNLOAD")},
            **extra,
        }
        return subprocess.run(
            ["bash", str(self.script)],
            env=env,
            capture_output=True,
            **UTF8_TEXT,
            timeout=FALLBACK_TIMEOUT_SECS,
            check=False,
        )

    def assert_fell_through(self, result: subprocess.CompletedProcess[str], reason: str) -> None:
        assert result.returncode == 0, result.stderr
        warnings = [line for line in result.stdout.splitlines() if line.startswith("::warning")]
        assert len(warnings) == 1 and warnings[0].startswith(WARNING), result.stdout
        assert reason in warnings[0]
        assert ERROR not in result.stdout
        assert list(self.temp.iterdir()) == []


@pytest.fixture
def seed(tmp_path: Path, seed_steps: dict[str, dict]) -> Seed:
    if not sys.platform.startswith("linux"):
        pytest.skip("the Linux seed step reads /etc/os-release and uses GNU tools")
    return Seed(tmp_path, seed_steps["bash"])


def test_a_matching_archive_is_installed_and_marked(seed: Seed) -> None:
    result = seed.run()
    assert result.returncode == 0, result.stderr
    assert "::warning" not in result.stdout and ERROR not in result.stdout
    assert f"seeded Python {_VERSION} (x64) at {seed.dest}" in result.stdout
    assert seed.marker.is_file()
    assert (seed.dest / "bin" / "python3.12").is_file()
    # The installer ran from the unpacked directory with its lib on the loader path.
    loader = (seed.dest / "loader-path.txt").read_text(encoding="utf-8")
    assert re.fullmatch(
        rf"loader path: {re.escape(str(seed.temp))}/seed-python\.\w+/unpacked/lib\n", loader
    )
    assert seed.log("order.log") == ["sha256sum", "tar"]
    assert list(seed.temp.iterdir()) == []
    (call,) = seed.log("curl.log")
    assert call.endswith(seed.url)
    assert "--proto =https" in call and "--tlsv1.2" in call and "--fail" in call
    assert not re.search(r"(?i)authorization|token|-H |--header", call)


def test_a_mismatched_archive_fails_the_job_and_is_never_opened(seed: Seed) -> None:
    wrong = "0" * 64
    seed.write_table(sha256=wrong)
    result = seed.run()
    assert result.returncode == 1
    errors = [line for line in result.stdout.splitlines() if line.startswith("::error")]
    assert len(errors) == 1 and errors[0].startswith(ERROR)
    assert f"expected {wrong}, got {seed.sha256}" in errors[0]
    assert "::warning" not in result.stdout
    assert seed.log("order.log") == ["sha256sum"]
    assert list(seed.tool_cache.iterdir()) == []
    assert list(seed.temp.iterdir()) == []


def test_an_installer_that_fails_halfway_leaves_no_tree_and_falls_through(seed: Seed) -> None:
    result = seed.run(FIXTURE_FAIL="1")
    seed.assert_fell_through(result, f"the verified Python {_VERSION} archive did not install")
    # The fixture installer had copied the tree before it failed.
    assert seed.log("order.log") == ["sha256sum", "tar"]
    assert not seed.dest.exists() and not seed.marker.exists()


def test_a_failed_download_is_retried_a_bounded_number_of_times_then_falls_through(
    seed: Seed,
) -> None:
    result = seed.run(STUB_CURL_FAIL="1")
    attempts = int(seed.step["env"]["SEED_DOWNLOAD_ATTEMPTS"])
    seed.assert_fell_through(result, f"failed {attempts} times")
    assert len(seed.log("curl.log")) == attempts
    assert seed.log("order.log") == []
    assert list(seed.tool_cache.iterdir()) == []


def test_a_runner_with_no_row_is_not_seeded(seed: Seed) -> None:
    seed.write_table(sha256=seed.sha256, os_version="0.0-not-this-host")
    result = seed.run()
    seed.assert_fell_through(result, f"no pinned Python 3.12 for linux {_host_os_version()} x64")
    assert seed.log("curl.log") == []
    assert list(seed.tool_cache.iterdir()) == []


def test_a_version_the_table_does_not_hold_is_not_seeded(seed: Seed) -> None:
    result = seed.run(PYTHON_SPEC="3.1")
    seed.assert_fell_through(result, "no pinned Python 3.1 for linux")
    assert seed.log("curl.log") == []


def test_an_unknown_architecture_is_not_seeded(seed: Seed) -> None:
    result = seed.run(RUNNER_ARCH="RISCV64")
    seed.assert_fell_through(result, "no pinned Python for RUNNER_ARCH=RISCV64")
    assert seed.log("curl.log") == []


def test_an_install_already_in_the_tool_cache_is_left_alone(seed: Seed) -> None:
    seed.dest.mkdir(parents=True)
    (seed.dest / "kept").write_text("kept\n", encoding="utf-8")
    seed.marker.touch()
    result = seed.run()
    seed.assert_fell_through(result, f"Python {_VERSION} (x64) is already in the tool cache")
    assert seed.log("curl.log") == []
    assert (seed.dest / "kept").is_file() and seed.marker.is_file()


def test_a_tools_directory_override_is_not_seeded(seed: Seed) -> None:
    result = seed.run(AGENT_TOOLSDIRECTORY=str(seed.tool_cache))
    seed.assert_fell_through(result, "AGENT_TOOLSDIRECTORY is set")
    assert seed.log("curl.log") == []
