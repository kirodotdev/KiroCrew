"""Behavioural tests for .github/workflows/memory-benchmark.yml.

The workflow's decisions live in shell embedded in `run:` blocks, so these tests
extract each step's script and execute it for real with `git`/`gh`/`kirocrew`
replaced by stubs. Three properties are verified rather than assumed, because
each one has a failure mode that produces a plausible-looking wrong answer:

* the FIRST-RUN PATH must say it is establishing a baseline, not print a delta
  against a file that does not exist -- a comparison with a missing side is the
  exact shape that manufactured a false "improvement" in this harness before;
* the ACCEPT step must detect a brand-new (untracked) baseline tree, since
  `git diff` alone ignores untracked files and the bootstrap baseline would
  otherwise read as "no changes" and never be committed;
* the run must never pass `--toy-embedder`, which would make the whole lane
  measure term overlap instead of semantic recall while still producing numbers.

Skipped where the POSIX toolchain the scripts need is unavailable, matching the
guards in test_issue_summary_workflow.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "memory-benchmark.yml"

pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists() or os.name == "nt" or shutil.which("bash") is None,
    reason="requires the workflow file plus a POSIX bash",
)


def _doc() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step(name_fragment: str, job: str = "measure") -> dict:
    for step in _doc()["jobs"][job]["steps"]:
        if name_fragment.lower() in str(step.get("name", "")).lower():
            return step
    raise AssertionError(f"no step in job {job!r} whose name contains {name_fragment!r}")


def _run(script: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    full = dict(os.environ)
    full.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env=full,
        capture_output=True,
        text=True,
    )


def _stub_dir(tmp_path: Path, **commands: str) -> Path:
    """Create a PATH directory of executable stubs."""
    d = tmp_path / "stubs"
    d.mkdir(exist_ok=True)
    for name, body in commands.items():
        p = d / name
        p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
        p.chmod(0o755)
    return d


# ── The lane must measure the real thing ─────────────────────────────────────


def test_the_run_never_uses_the_toy_embedder() -> None:
    """A toy-embedder run yields numbers that describe term overlap, not recall.

    The harness labels such reports, but a nightly that silently produced them
    would still fill the trend line with values nobody could act on.
    """
    # Comments in this workflow deliberately mention the flag to explain why it
    # is absent, so the check has to look at effective lines only -- a substring
    # search over the whole file fails on its own documentation.
    effective = [
        ln
        for ln in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    ]
    assert not any("--toy-embedder" in ln for ln in effective)


def test_the_model_cache_key_is_the_pinned_digest() -> None:
    """Keyed on the digest so a model swap invalidates the cache automatically.

    A version-string key would keep serving the old weights after a swap, and
    the resulting numbers would be compared against a baseline from a different
    vector space.
    """
    cache = _step("Cache the embedding model")
    assert cache["with"]["key"] == "gguf-${{ steps.model.outputs.digest }}"


def test_the_digest_guard_rejects_a_non_hex_value(tmp_path: Path) -> None:
    """A silently-empty digest would produce the cache key `gguf-`, shared by
    every future model. The step validates the shape before using it."""
    script = _step("Read the pinned embedding-model digest")["run"]
    # Replace the python3 read with a stub that emits junk, keeping the guard.
    broken = script.replace(
        "DIGEST=\"$(python3 -c 'from kiro_crew import embeddings; "
        "print(embeddings._GGUF_SHA256)')\"",
        'DIGEST="not-a-digest"',
    )
    assert broken != script, "the digest read line changed; update this test"
    out = _run(broken, tmp_path, {"GITHUB_OUTPUT": str(tmp_path / "out"), "HOME": str(tmp_path)})
    assert out.returncode != 0
    assert "could not read the model digest" in out.stdout + out.stderr


# ── First run must not compare against a file that is not there ──────────────


def test_first_run_reports_bootstrap_instead_of_a_delta(tmp_path: Path) -> None:
    script = _step("Compare against the accepted baseline", job="accept")["run"]
    (tmp_path / "bench_baselines" / "latest").mkdir(parents=True)
    (tmp_path / "bench_baselines" / "latest" / "locomo10_now.json").write_text("{}")
    stubs = _stub_dir(tmp_path, kirocrew='echo "SHOULD NOT RUN"; exit 1')

    out = _run(
        script,
        tmp_path,
        {
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        },
    )
    assert out.returncode == 0, out.stderr
    body = (tmp_path / "comparison.md").read_text(encoding="utf-8")
    assert "establishes one" in body
    # `compare` must not have been invoked with a missing baseline.
    assert "SHOULD NOT RUN" not in body


def test_an_existing_baseline_is_actually_compared(tmp_path: Path) -> None:
    script = _step("Compare against the accepted baseline", job="accept")["run"]
    for sub in ("latest", "accepted"):
        d = tmp_path / "bench_baselines" / sub
        d.mkdir(parents=True)
        (d / "locomo10_now.json").write_text("{}")
    stubs = _stub_dir(tmp_path, kirocrew='echo "COMPARED $*"')

    out = _run(
        script,
        tmp_path,
        {
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        },
    )
    assert out.returncode == 0, out.stderr
    body = (tmp_path / "comparison.md").read_text(encoding="utf-8")
    assert "COMPARED bench compare" in body
    assert "establishes one" not in body


# ── The accept step must see an untracked baseline ───────────────────────────


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True, capture_output=True)
    # A real bare remote, because the step under test runs `git push`. Stubbing
    # git instead would exercise the stub rather than the script.
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


@pytest.mark.skipif(shutil.which("git") is None, reason="requires git")
def test_a_brand_new_baseline_tree_is_detected_and_committed(tmp_path: Path) -> None:
    """The bootstrap case: `accepted/` does not exist in git yet.

    `git diff --quiet` (without `--cached`) ignores untracked files, so the very
    first baseline would look unchanged and never land. Staging first is what
    makes it visible.
    """
    repo = _git_repo(tmp_path)
    latest = repo / "bench_baselines" / "latest"
    latest.mkdir(parents=True)
    (latest / "locomo10_now.json").write_text('{"metrics": {}}')

    script = _step("Open a PR to accept the new baseline", job="accept")["run"]
    stubs = _stub_dir(
        tmp_path,
        gh=(
            'echo "gh $*" >> "$PWD/gh-calls.log"\n'
            '# `pr view` failing is how the script learns no PR is open yet;\n'
            '# `pr create` must then succeed or the step legitimately fails.\n'
            'case "$*" in\n'
            '  *"pr view"*) exit 1 ;;\n'
            '  *) exit 0 ;;\n'
            'esac\n'
        ),
    )
    out = _run(
        script,
        repo,
        {"PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "GH_TOKEN": "x"},
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "nothing to accept" not in out.stdout
    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "bench_baselines/accepted/locomo10_now.json" in committed
    calls = (repo / "gh-calls.log").read_text(encoding="utf-8")
    assert "pr create" in calls, "the accept step never tried to open a PR"


@pytest.mark.skipif(shutil.which("git") is None, reason="requires git")
def test_an_unchanged_baseline_opens_no_pr(tmp_path: Path) -> None:
    """A nightly that opened an identical PR every day would train people to
    ignore it."""
    repo = _git_repo(tmp_path)
    payload = '{"metrics": {}}'
    for sub in ("latest", "accepted"):
        d = repo / "bench_baselines" / sub
        d.mkdir(parents=True)
        (d / "locomo10_now.json").write_text(payload)
    subprocess.run(
        ["git", "add", "bench_baselines/accepted"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-qm", "accept baseline"], cwd=repo, check=True, capture_output=True
    )

    script = _step("Open a PR to accept the new baseline", job="accept")["run"]
    stubs = _stub_dir(tmp_path, gh='echo "gh SHOULD NOT RUN"; exit 1')
    out = _run(
        script,
        repo,
        {"PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "GH_TOKEN": "x"},
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "nothing to accept" in out.stdout
    assert "SHOULD NOT RUN" not in out.stdout


# ── Schedule and safety shape ───────────────────────────────────────────────


def test_the_job_is_single_flight_and_bounded() -> None:
    """Two concurrent runs would double the load for no extra signal."""
    doc = _doc()
    assert doc["concurrency"]["cancel-in-progress"] is True
    assert doc["jobs"]["measure"]["timeout-minutes"] <= 90


def test_the_arms_run_as_a_matrix_not_sequentially() -> None:
    """Measured: ~55 min for one 10-instance arm, so two in one job overruns any
    timeout worth setting. One arm per job is what keeps the budget honest."""
    measure = _doc()["jobs"]["measure"]
    timelines = measure["strategy"]["matrix"]["timeline"]
    assert set(timelines) == {"now", "anchored"}
    # A failed arm must not cancel its sibling: a partial result is still worth
    # reading, and the accept job tolerates a missing file.
    assert measure["strategy"]["fail-fast"] is False


def test_the_model_is_downloaded_before_the_run(tmp_path: Path) -> None:
    """`bench retrieval` only WAITS for the model; it never fetches it.

    Without an explicit download step a cache miss makes every arm refuse, so the
    first-ever run -- and every run after a model swap or cache eviction -- could
    never bootstrap and the nightly would fail forever instead of trending. This
    was a real defect in the first version of this workflow.
    """
    steps = _doc()["jobs"]["measure"]["steps"]
    names = [str(s.get("name", "")) for s in steps]
    ensure_idx = next(
        i for i, n in enumerate(names) if "ensure the embedding model" in n.lower()
    )
    run_idx = next(i for i, n in enumerate(names) if n.lower() == "run the benchmark")
    cache_idx = next(i for i, n in enumerate(names) if "cache the embedding model" in n.lower())
    assert cache_idx < ensure_idx < run_idx, (
        "order matters: restore the cache, then fill a miss, then measure"
    )
    body = steps[ensure_idx]["run"]
    assert "ensure_model" in body
    # A failed download must fail the job -- a silent skip would leave the arm to
    # refuse later with a much less obvious message.
    assert "sys.exit(1)" in body


def test_the_download_step_does_not_inherit_the_test_suite_skip_flag() -> None:
    """`KIROCREW_SKIP_MODEL_DOWNLOAD` exists so unit tests never pull 639 MB.

    Here the download is the entire point, so the step states the empty value
    rather than leaving it to whatever the environment happens to carry.
    """
    step = _step("Ensure the embedding model is resident")
    assert step["env"]["KIROCREW_SKIP_MODEL_DOWNLOAD"] == ""


def test_the_model_dir_is_derived_from_the_code_not_hardcoded() -> None:
    """A hardcoded path silently stops covering the model if the code moves it."""
    run = _step("Read the pinned embedding-model digest")["run"]
    assert "default_model_path()" in run


def test_the_corpus_slice_is_a_constant_not_a_per_run_input() -> None:
    """Every run must measure the same corpus.

    If the slice were tunable per run, a full-corpus dispatch would be accepted
    as the baseline and every later sliced run would report "not comparable" --
    a nightly that cries wolf nightly is a nightly nobody reads.
    """
    doc = _doc()
    assert doc["env"]["BENCH_INSTANCES"] == "5"
    # YAML 1.1 resolves a bare `on` to the BOOLEAN True, so PyYAML keys the
    # trigger block under True rather than "on". Accept either so this test is
    # about the workflow, not about which YAML version the loader implements.
    triggers = doc.get("on", doc.get(True))
    assert triggers is not None, "could not find the trigger block"
    dispatch = triggers["workflow_dispatch"]
    assert not (dispatch or {}), f"workflow_dispatch should take no inputs, got {dispatch!r}"
    run = _step("Run the benchmark")["run"]
    assert "--instances $INSTANCES" in run


def test_accept_runs_even_when_an_arm_fails() -> None:
    accept = _doc()["jobs"]["accept"]
    assert accept["needs"] == "measure"
    assert "always()" in str(accept["if"])


def test_every_arm_failing_is_reported_as_such(tmp_path: Path) -> None:
    """An empty report set must say so rather than render an all-clear summary.

    A silent empty comparison is the same failure shape as a check that never ran
    being displayed as a check that passed.
    """
    script = _step("Compare against the accepted baseline", job="accept")["run"]
    (tmp_path / "bench_baselines" / "latest").mkdir(parents=True)
    stubs = _stub_dir(tmp_path, kirocrew='echo "SHOULD NOT RUN"; exit 1')
    out = _run(
        script,
        tmp_path,
        {
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        },
    )
    assert out.returncode == 0, out.stderr
    body = (tmp_path / "comparison.md").read_text(encoding="utf-8")
    assert "every arm failed" in body


def test_write_scope_is_confined_to_the_job_that_opens_the_pr() -> None:
    """The measuring jobs never need write scope; only `accept` does."""
    doc = _doc()
    assert doc["permissions"] == {"contents": "read"}
    assert "permissions" not in doc["jobs"]["measure"]
    accept_perms = doc["jobs"]["accept"]["permissions"]
    assert accept_perms["contents"] == "write"
    assert accept_perms["pull-requests"] == "write"


def test_every_third_party_action_is_pinned_to_a_sha() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    uses = [ln.split("uses:", 1)[1].strip() for ln in text.splitlines() if "uses:" in ln]
    assert uses, "no actions found; the parse is wrong"
    for ref in uses:
        pin = ref.split("@", 1)[1].split()[0]
        assert len(pin) == 40 and all(c in "0123456789abcdef" for c in pin), (
            f"{ref} is not pinned to a full commit sha"
        )
