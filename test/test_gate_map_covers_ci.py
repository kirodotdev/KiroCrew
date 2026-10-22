"""``gate_map.json`` is pinned to CI: a gate mirrors a CI step, a CI check is not forgotten.

``pre_push_gate.py`` runs what the map says. A map that drifts from the workflows would
write a proof for checks CI has dropped, or leave out one CI has added. Three
pins keep it honest: every gate names CI text that is still in the workflow, every
script a gate runs exists, and every ``check_*.py`` the workflows run is either a gate
or a ``ci_only`` entry with a reason.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = (
    ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-prepare-pr"
    / "scripts"
)
WORKFLOWS = ROOT / ".github" / "workflows"
GATE_MAP = json.loads((SCRIPTS / "gate_map.json").read_text(encoding="utf-8"))

# The CI steps this map has to account for: the repository's own checks.
_CI_CHECK = re.compile(
    r"scripts/(?:check_[a-z_]+\.py|docs_lint\.py|docs-lint\.sh|verify_vendor_manifest\.py)"
)


def _workflow_text() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(WORKFLOWS.glob("*.yml"))}


def _gate_ids() -> list[str]:
    return [g["id"] for g in GATE_MAP["gates"]]


def test_gate_ids_are_unique_and_never_also_ci_only() -> None:
    ids = _gate_ids() + [e["id"] for e in GATE_MAP["ci_only"]]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("gate", GATE_MAP["gates"], ids=_gate_ids())
def test_every_gate_names_text_that_is_still_in_its_ci_workflow(gate) -> None:
    workflow = WORKFLOWS / gate["ci"]["file"]
    assert workflow.is_file(), "{} names no workflow".format(gate["id"])
    assert gate["ci"]["text"] in workflow.read_text(
        encoding="utf-8"
    ), "{}: CI no longer contains {!r}; update the gate or move it to ci_only".format(
        gate["id"], gate["ci"]["text"]
    )


@pytest.mark.parametrize("gate", GATE_MAP["gates"], ids=_gate_ids())
def test_every_gate_runs_a_file_that_exists_and_the_text_it_mirrors(gate) -> None:
    for token in gate["cmd"]:
        if token.startswith("scripts/"):
            assert (ROOT / token).is_file(), "{}: {} is not in the repository".format(
                gate["id"], token
            )
    if gate["ci"]["text"].startswith(("scripts/", "./scripts/")):
        assert gate["ci"]["text"].removeprefix("./") in gate["cmd"]


@pytest.mark.parametrize(
    "gate", [g for g in GATE_MAP["gates"] if g.get("base_env")], ids=lambda g: g["id"]
)
def test_a_gate_that_takes_a_base_reads_the_same_variable_as_ci(gate) -> None:
    assert gate["base_env"] in (WORKFLOWS / gate["ci"]["file"]).read_text(encoding="utf-8")


def test_every_check_ci_runs_is_a_gate_or_ci_only_with_a_reason() -> None:
    covered = " ".join(" ".join(g["cmd"]) for g in GATE_MAP["gates"])
    covered += " " + " ".join(e["needle"] for e in GATE_MAP["ci_only"])
    for gate in GATE_MAP["gates"]:
        if isinstance(gate.get("self_test"), list):
            covered += " " + " ".join(gate["self_test"])
    missing = sorted(
        {
            name
            for text in _workflow_text().values()
            for name in _CI_CHECK.findall(text)
            if name.removeprefix("./") not in covered
        }
    )
    assert not missing, (
        "CI runs {} and gate_map.json neither runs nor lists it: add a gate, or a "
        "ci_only entry with a reason".format(missing)
    )


@pytest.mark.parametrize("entry", GATE_MAP["ci_only"], ids=lambda e: e["id"])
def test_every_ci_only_entry_has_a_reason_and_is_still_in_ci(entry) -> None:
    assert len(entry["reason"]) >= 20
    assert any(
        entry["needle"] in text for text in _workflow_text().values()
    ), "{}: no workflow contains {!r}; delete the entry".format(entry["id"], entry["needle"])


_PATH_GATED = [g for g in GATE_MAP["gates"] if g.get("paths")]
_REPO_ROOTS = r"(?:src|test|scripts|\.github|website|docs|packages|docker|packaging|semgrep)"
_REPO_PATH = re.compile(r"^" + _REPO_ROOTS + r"/[^\s]+$")
_ROOT_LIST_NAME = re.compile(r"(TARGETS|ROOTS?|DIRS|SCAN)")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def _inputs_the_script_names(script: Path) -> list[str]:
    """Repo paths a checker opens or compares: path literals, root lists, baseline files.

    Docstrings are prose and are skipped. A bare name counts only when it is assigned to a
    target or root list (``DEFAULT_TARGETS``, ``SCAN_ROOT``) and exists in the repository, so
    a word such as ``"test"`` in a set of path parts is not mistaken for an input.
    """
    source = script.read_text(encoding="utf-8")
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            if _REPO_PATH.match(node.value):
                found.append(node.value)
            elif node.value.endswith("-baseline.txt"):
                found.append(".github/" + node.value.removeprefix(".github/"))
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and _ROOT_LIST_NAME.search(t.id) for t in node.targets
        ):
            found += [
                n.value
                for n in ast.walk(node.value)
                if isinstance(n, ast.Constant)
                and isinstance(n.value, str)
                and (ROOT / n.value).exists()
            ]
    if "ratchet_scope" in source:
        found.append("scripts/ratchet_scope.py")
    return found


def _probe(path: str) -> str:
    """A file under ``path`` when it names a directory, else ``path`` itself."""
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return path if "." in name and not path.endswith("/") else path.rstrip("/") + "/probe.py"


@pytest.mark.parametrize("gate", _PATH_GATED, ids=lambda g: g["id"])
def test_a_path_gated_gate_paths_cover_every_input_its_script_names(gate) -> None:
    """A gate that is filtered by path must be selected by everything its checker reads.

    The filter is derived from the checker, not guessed: a path, root list or baseline file
    the script names that no pattern matches would give a false green proof for a change to it.
    """
    patterns = [re.compile(p) for p in gate["paths"]]
    inputs: list[str] = []
    for token in gate["cmd"]:
        if token.startswith("scripts/") and token.endswith(".py"):
            inputs += [token] + _inputs_the_script_names(ROOT / token)
        elif token.startswith("-") or token in ("{py}", "bash") or token == "-m":
            continue
        elif (ROOT / token).exists():
            inputs.append(token)  # a tool run on named directories and files
    if gate["cmd"][1:3] == ["-m", gate.get("module")]:
        inputs += ["pyproject.toml", "setup.cfg"]  # isort, flake8 and mypy read their config
    unselected = [i for i in inputs if not any(p.search(_probe(i)) for p in patterns)]
    assert not unselected, "{}: paths miss inputs {}".format(gate["id"], sorted(set(unselected)))


@pytest.mark.parametrize("gate", _PATH_GATED, ids=lambda g: g["id"])
def test_only_a_measured_slow_gate_has_a_path_filter(gate) -> None:
    assert re.match(r"^\d+(\.\d+)? s", gate.get("slow", "")), (
        "{}: a gate runs on every diff unless it has a measured 'slow' time; "
        "add the measurement or drop 'paths'".format(gate["id"])
    )
    assert float(gate["slow"].split()[0]) >= 10 or "cold" in gate["slow"]


def test_the_cheap_gates_the_findings_named_have_no_path_filter() -> None:
    by_id = {g["id"]: g for g in GATE_MAP["gates"]}
    for gate_id in ("black", "acp-frame-host-data"):
        assert "paths" not in by_id[gate_id], gate_id
