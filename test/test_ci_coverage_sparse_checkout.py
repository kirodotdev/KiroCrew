"""Coverage Combine and Coverage Gate check out only the paths they read.

Both jobs decide whether a pull request may merge, so a sparse list that falls
behind the tree has to turn the job red, never let it pass on less. Two things
make that hold, and both are pinned here:

* Coverage Combine: ``coverage xml`` and ``coverage report`` stop with "No
  source for code" on a measured file that is not on disk, unless errors are
  ignored. So the report commands must not ignore errors, and every root a shard
  measures (each ``--cov`` target, each ``[coverage:paths]`` canonical root)
  must be inside the sparse list.
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
GATE_SCRIPT = "scripts/check_per_file_coverage.py"

EXPECTED = {
    "coverage-combine": ["/src/", "/setup.cfg", "/pyproject.toml"],
    "coverage-gate": [
        "/scripts/check_per_file_coverage.py",
        "/.github/coverage-baselines/",
        "/pyproject.toml",
    ],
}


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


@pytest.mark.parametrize("job", sorted(EXPECTED))
def test_the_job_checks_out_exactly_the_listed_paths(job: str) -> None:
    checkout = _checkout(job)["with"]
    assert _patterns(job) == EXPECTED[job]
    assert checkout["sparse-checkout-cone-mode"] is False
    assert checkout["persist-credentials"] is False


@pytest.mark.parametrize("job", sorted(EXPECTED))
def test_every_listed_path_is_anchored_and_exists(job: str) -> None:
    for pattern in _patterns(job):
        assert pattern.startswith("/"), f"{pattern} would match at any depth"
        assert not set("*?[!") & set(pattern), f"{pattern} is a glob, not a path"
        on_disk = ROOT / pattern.strip("/")
        assert on_disk.is_dir() if pattern.endswith("/") else on_disk.is_file(), pattern


@pytest.mark.parametrize("job", sorted(EXPECTED))
def test_the_checkout_comes_before_every_artifact_download(job: str) -> None:
    steps = _steps(job)
    checkout_at = steps.index(_checkout(job))
    downloads = [i for i, s in enumerate(steps) if _uses(s, "actions/download-artifact")]
    assert downloads, f"{job} downloads no artifact; this test measures nothing"
    assert checkout_at < min(downloads)


@pytest.mark.parametrize("job", sorted(EXPECTED))
def test_the_job_uses_no_action_from_the_tree(job: str) -> None:
    local = [s["uses"] for s in _steps(job) if str(s.get("uses", "")).startswith("./")]
    assert [u for u in local if not _covers(_patterns(job), u[2:])] == []


def test_combine_holds_every_root_the_shards_measure() -> None:
    patterns = _patterns("coverage-combine")
    roots = _measured_roots()
    assert "src/kiro_crew" in roots
    assert [root for root in roots if not _covers(patterns, root)] == []


def test_combine_holds_the_files_its_tools_read() -> None:
    patterns = _patterns("coverage-combine")
    (setup_uv,) = [s for s in _steps("coverage-combine") if _uses(s, "astral-sh/setup-uv")]
    hashed = str(setup_uv["with"]["cache-dependency-glob"]).split()
    assert "setup.cfg" in hashed
    assert [path for path in hashed if not _covers(patterns, path)] == []


def test_combine_reports_a_missing_source_file_as_an_error() -> None:
    run = _run_text("coverage-combine")
    commands = [line.strip() for line in run.splitlines() if line.strip().startswith("coverage ")]
    assert [c.split()[1] for c in commands] == ["combine", "xml", "report"]
    for command in commands:
        assert not re.search(r"(^|\s)(-i|--ignore-errors)(\s|$)", command), command
    config = _coverage_config()
    assert not config.has_option("coverage:report", "ignore_errors")
    assert run.index("test -f setup.cfg ||") < run.index("coverage combine")


def test_gate_holds_every_repository_path_its_steps_name() -> None:
    patterns = _patterns("coverage-gate")
    named = sorted(
        set(re.findall(r"(?<![\w./-])((?:scripts|\.github)/[\w./-]+)", _run_text("coverage-gate")))
    )
    assert GATE_SCRIPT in named
    assert ".github/coverage-baselines/backend.txt" in named
    assert ".github/coverage-baselines/frontend.txt" in named
    assert [path for path in named if not _covers(patterns, path)] == []
    assert [path for path in named if not (ROOT / path).is_file()] == []


def test_gate_holds_every_module_its_script_imports_from_the_tree() -> None:
    patterns = _patterns("coverage-gate")
    assert [path for path in _local_imports(GATE_SCRIPT) if not _covers(patterns, path)] == []


def test_gate_holds_the_file_setup_uv_hashes() -> None:
    (setup_uv,) = [s for s in _steps("coverage-gate") if _uses(s, "astral-sh/setup-uv")]
    assert "cache-dependency-glob" not in setup_uv.get("with", {})
    assert _covers(_patterns("coverage-gate"), "pyproject.toml")


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


def test_a_local_import_of_the_gate_script_would_be_found(tmp_path, monkeypatch) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "gate.py").write_text(
        "import os\nimport helper\nfrom . import sibling\nfrom rootmod import x\n", encoding="utf-8"
    )
    (tmp_path / "scripts" / "helper.py").write_text("", encoding="utf-8")
    (tmp_path / "scripts" / "sibling.py").write_text("", encoding="utf-8")
    (tmp_path / "rootmod").mkdir()
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    assert _local_imports("scripts/gate.py") == [
        "scripts/helper.py",
        "rootmod",
        "scripts/sibling.py",
    ]
