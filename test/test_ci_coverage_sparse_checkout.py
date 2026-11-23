"""Coverage Combine and Coverage Gate check out only the paths they read.

Both jobs decide whether a pull request may merge, so a sparse list that falls
behind the tree has to turn the job red, never let it pass on less. Two things
make that hold, and both are pinned here:

* Coverage Combine: ``coverage xml`` and ``coverage report`` stop with "No
  source for code" on a measured file that is not on disk, unless errors are
  ignored. So nothing in the step may ignore errors, and every root a shard
  measures (each ``--cov`` target, each ``[coverage:paths]`` canonical root)
  must be inside the sparse list, with the script that runs the two commands.
* Coverage Gate: a path the job runs or reads that is missing from the list
  either exits non-zero (the script) or reads as an empty baseline, which is
  the strictest one. So every repository path its steps name, and every module
  the script imports from this repository, must be inside the list.
"""

from __future__ import annotations

import ast
import re
from configparser import ConfigParser
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_TEXT = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
JOBS = yaml.safe_load(CI_TEXT)["jobs"]
SPARSE_JOBS = ("coverage-combine", "coverage-gate")


def _steps(job: str) -> list[dict]:
    return JOBS[job]["steps"]


def _uses(step: dict, action: str) -> bool:
    return str(step.get("uses", "")).startswith(action + "@")


def _checkout(job: str) -> dict:
    (step,) = [s for s in _steps(job) if _uses(s, "actions/checkout")]
    return step


def _patterns(job: str) -> list[str]:
    return str(_checkout(job)["with"]["sparse-checkout"]).split()


def _covers(patterns: list[str], path: str) -> bool:
    """Whether a repo-relative POSIX ``path`` is inside an anchored pattern list.

    A pattern ending in ``/`` is a directory and holds everything below it; any
    other pattern is one file.
    """
    target = "/" + path.strip("/")
    for pattern in patterns:
        if pattern.endswith("/"):
            if (target + "/").startswith(pattern):
                return True
        elif target == pattern:
            return True
    return False


def _run_text(job: str) -> str:
    return "\n".join(str(s.get("run", "")) for s in _steps(job))


def _coverage_config() -> ConfigParser:
    config = ConfigParser()
    config.read(ROOT / "setup.cfg", encoding="utf-8")
    return config


def _path_aliases() -> dict[str, list[str]]:
    config = _coverage_config()
    return {name: value.split() for name, value in config.items("coverage:paths")}


def _measured_roots() -> list[str]:
    """Every root the shards measure, as a repo-relative directory.

    A ``--cov`` target with a slash is already a path. A bare one is a package
    name: it is the directory of that name under a ``[coverage:paths]``
    canonical root, or the canonical root of the alias group that lists it.
    """
    aliases = _path_aliases()
    canonical = [entries[0] for entries in aliases.values()]
    roots = list(canonical)
    targets = sorted(set(re.findall(r"--cov=([\w./-]+)", CI_TEXT)))
    assert targets, "ci.yml no longer passes --cov; this test measures nothing"
    for target in targets:
        if "/" in target:
            roots.append(target)
            continue
        found = [entries[0] for entries in aliases.values() if target in entries[1:]]
        found += [f"{root.rstrip('/')}/{target}" for root in canonical]
        found = [candidate for candidate in found if (ROOT / candidate).is_dir()]
        assert found, f"--cov={target} names no directory this test can resolve"
        roots.extend(found)
    return sorted(set(roots))


def _local_imports(script: str) -> list[str]:
    """Repo-relative paths a script's imports could load from this repository."""
    tree = ast.parse((ROOT / script).read_text(encoding="utf-8"))
    script_dir = Path(script).parent.as_posix()
    names: set[tuple[str, bool]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update((alias.name.split(".")[0], False) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative = (node.module or node.names[0].name).split(".")[0]
                names.add((relative, True))
            elif node.module:
                names.add((node.module.split(".")[0], False))
    found = []
    for name, relative in sorted(names):
        bases = [script_dir] if relative else [script_dir, "."]
        for base in bases:
            for candidate in (f"{base}/{name}.py", f"{base}/{name}"):
                if (ROOT / candidate).exists():
                    found.append(Path(candidate).as_posix())
    return found


def _hashed_paths(job: str) -> list[str]:
    """The literal files setup-uv hashes for its cache key in ``job``.

    With no ``cache-dependency-glob`` the action's default applies, and the one
    file of this repository it matches at the root is ``pyproject.toml``.
    Wildcard entries match whatever is checked out, so they demand nothing.
    """
    (setup_uv,) = [s for s in _steps(job) if _uses(s, "astral-sh/setup-uv")]
    glob = setup_uv.get("with", {}).get("cache-dependency-glob")
    entries = str(glob).split() if glob else ["pyproject.toml"]
    return [entry for entry in entries if not set("*?[") & set(entry)]


def _named_paths(job: str) -> set[str]:
    """Every ``scripts/`` or ``.github/`` path the job's run steps name."""
    pattern = r"(?<![\w./-])((?:scripts|\.github)/[\w./-]+)"
    return set(re.findall(pattern, _run_text(job)))


@pytest.mark.parametrize("job", SPARSE_JOBS)
def test_the_checkout_is_sparse_and_keeps_no_credentials(job: str) -> None:
    checkout = _checkout(job)["with"]
    assert _patterns(job), f"{job} lists no sparse path, so it checks out the whole tree"
    assert checkout["sparse-checkout-cone-mode"] is False
    assert checkout["persist-credentials"] is False


@pytest.mark.parametrize("job", SPARSE_JOBS)
def test_every_listed_path_is_anchored_and_exists(job: str) -> None:
    for pattern in _patterns(job):
        assert pattern.startswith("/"), f"{pattern} would match at any depth"
        assert not set("*?[!") & set(pattern), f"{pattern} is a glob, not a path"
        on_disk = ROOT / pattern.strip("/")
        assert on_disk.is_dir() if pattern.endswith("/") else on_disk.is_file(), pattern


@pytest.mark.parametrize("job", SPARSE_JOBS)
def test_the_checkout_comes_before_every_artifact_download(job: str) -> None:
    steps = _steps(job)
    checkout_at = steps.index(_checkout(job))
    downloads = [i for i, s in enumerate(steps) if _uses(s, "actions/download-artifact")]
    assert downloads, f"{job} downloads no artifact; this test measures nothing"
    assert checkout_at < min(downloads)


@pytest.mark.parametrize("job", SPARSE_JOBS)
def test_the_job_uses_no_action_the_checkout_leaves_out(job: str) -> None:
    local = {s["uses"] for s in _steps(job) if str(s.get("uses", "")).startswith("./")}
    assert {u for u in local if not _covers(_patterns(job), u[2:])} == set()


@pytest.mark.parametrize("job", SPARSE_JOBS)
def test_the_checkout_holds_the_files_setup_uv_hashes(job: str) -> None:
    hashed = _hashed_paths(job)
    assert hashed, f"{job} hashes no literal file; this test measures nothing"
    assert {path for path in hashed if not _covers(_patterns(job), path)} == set()


def test_combine_holds_every_root_the_shards_measure() -> None:
    patterns = _patterns("coverage-combine")
    roots = _measured_roots()
    assert roots, "no measured root resolved; this test measures nothing"
    assert {root for root in roots if not _covers(patterns, root)} == set()


def test_combine_holds_the_coverage_configuration() -> None:
    assert _coverage_config().has_section("coverage:run")
    assert _covers(_patterns("coverage-combine"), "setup.cfg")


def test_combine_reports_a_missing_source_file_as_an_error() -> None:
    run = _run_text("coverage-combine")
    code = [line.split() for line in run.splitlines() if not line.lstrip().startswith("#")]
    for command in code:
        assert not {"-i", "--ignore-errors"} & set(command), " ".join(command)
    assert not _coverage_config().has_option("coverage:report", "ignore_errors")
    scripts = [path for path in _named_paths("coverage-combine") if path.endswith(".py")]
    assert scripts, "the combine step runs no report script; this test measures nothing"
    combine = run.index("coverage combine")
    assert all(combine < run.index(script) for script in scripts)
    guard = re.search(r"(?:test|\[)\s+-[efs]\s+setup\.cfg\b", run)
    assert guard and guard.start() < combine


@pytest.mark.parametrize("job", SPARSE_JOBS)
def test_the_checkout_holds_every_repository_path_the_steps_name(job: str) -> None:
    patterns = _patterns(job)
    named = _named_paths(job)
    assert named, f"{job} names no repository path; this test measures nothing"
    assert {path for path in named if not _covers(patterns, path)} == set()
    assert {path for path in named if not (ROOT / path).is_file()} == set()


@pytest.mark.parametrize("job", SPARSE_JOBS)
def test_the_checkout_holds_every_module_its_scripts_import_from_the_tree(job: str) -> None:
    patterns = _patterns(job)
    scripts = {path for path in _named_paths(job) if path.endswith(".py")}
    assert scripts, f"{job} runs no script; this test measures nothing"
    imported = {path for script in scripts for path in _local_imports(script)}
    assert {path for path in imported if not _covers(patterns, path)} == set()


@pytest.mark.parametrize(
    ("path", "covered"),
    [
        ("src/kiro_crew/agent.py", True),
        ("src", True),
        ("setup.cfg", True),
        ("test/src/kiro_crew/agent.py", False),
        ("srcs/x.py", False),
        ("sub/setup.cfg", False),
        ("conftest.py", False),
    ],
)
def test_the_cover_check_reads_patterns_as_anchored_paths(path: str, covered: bool) -> None:
    assert _covers(["/src/", "/setup.cfg"], path) is covered


def test_an_import_from_the_tree_is_found(tmp_path, monkeypatch) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "gate.py").write_text(
        "import os\nimport helper\nfrom . import sibling\nfrom rootmod import x\n", encoding="utf-8"
    )
    (tmp_path / "scripts" / "helper.py").write_text("", encoding="utf-8")
    (tmp_path / "scripts" / "sibling.py").write_text("", encoding="utf-8")
    (tmp_path / "rootmod").mkdir()
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    assert set(_local_imports("scripts/gate.py")) == {
        "scripts/helper.py",
        "scripts/sibling.py",
        "rootmod",
    }
