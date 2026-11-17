"""``scripts/check_metrics_schema_history.py`` fails CI when the metric manifest shrinks.

Each case runs the real script's ``main()`` in this interpreter against a
throwaway git repository that carries copies of the script, the rule module and a
small fixture manifest: one commit for the base, a working-tree edit for the
change. The script is loaded from the copy, so its ``ROOT`` is that repository.
No case starts a Python child; git is the only subprocess.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest
from git_maintenance_helpers import git_maintenance_off_entries

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = "scripts/check_metrics_schema_history.py"
_RULE = "src/kiro_crew/metrics/schema_manifest.py"
_MANIFEST = "src/kiro_crew/metrics/schema_manifest.json"

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}

_BASE: Dict[str, Any] = {
    "metrics": [
        {
            "name": "kirocrew.x.done",
            "kind": "counter",
            "unit": "1",
            "scope": "kiro_crew",
            "temporality": "delta",
            "status": "active",
            "attributes": {"outcome": {"values": ["error", "ok"]}},
        }
    ]
}


@pytest.fixture(autouse=True)
def _host_git_config_is_not_read(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    _floor_monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    for key, value in git_maintenance_off_entries(os.environ).items():
        _floor_monkeypatch.setenv(key, value)


def _run(cwd: Path, *args: str, env: Dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        env={**os.environ, **_GIT_IDENTITY, **(env or {})},
    )


def _write(repo: Path, doc: Dict[str, Any]) -> None:
    (repo / _MANIFEST).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for rel in (_SCRIPT, _RULE):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_REPO / rel, tmp_path / rel)
    _write(tmp_path, _BASE)
    for args in (("init", "-q"), ("add", "-A"), ("commit", "-q", "-m", "base")):
        assert _run(tmp_path, "git", *args).returncode == 0
    return tmp_path


class _Done:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _main(root: Path, args: List[str], capsys: pytest.CaptureFixture[str]) -> _Done:
    spec = importlib.util.spec_from_file_location("_metrics_schema_history_gate", root / _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    capsys.readouterr()
    code = module.main(args)
    out = capsys.readouterr()
    return _Done(code, out.out, out.err)


@pytest.fixture
def gate(repo: Path, capsys: pytest.CaptureFixture[str], _floor_monkeypatch: pytest.MonkeyPatch):
    def run(base: str = "HEAD") -> _Done:
        _floor_monkeypatch.setenv("METRICS_SCHEMA_BASE", base)
        return _main(repo, [], capsys)

    return run


def test_self_test_passes(capsys):
    done = _main(_REPO, ["--test"], capsys)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "probes ok" in done.stdout


def test_an_unchanged_manifest_passes(gate):
    done = gate()
    assert done.returncode == 0, done.stdout + done.stderr


def test_an_added_row_passes(repo, gate):
    new = copy.deepcopy(_BASE)
    new["metrics"].append(dict(copy.deepcopy(new["metrics"][0]), name="kirocrew.x.started"))
    _write(repo, new)
    done = gate()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "+1 rows" in done.stdout


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d.update(metrics=[]),
        lambda d: d["metrics"][0].update(name="kirocrew.x.finished"),
        lambda d: d["metrics"][0]["attributes"]["outcome"].update(values=["ok"]),
    ],
    ids=["removed row", "renamed row", "removed value"],
)
def test_a_shrinking_manifest_fails_naming_the_metric(repo, gate, edit):
    new = copy.deepcopy(_BASE)
    edit(new)
    _write(repo, new)
    done = gate()
    assert done.returncode == 1, done.stdout + done.stderr
    assert "kirocrew.x.done" in done.stdout


def test_the_rule_is_read_from_the_base(repo, gate):
    """A change that loosens the rule and drops a row in one go still fails."""
    (repo / _RULE).write_text("def is_additive(old, new):\n    return []\n", encoding="utf-8")
    _write(repo, {"metrics": []})
    done = gate()
    assert done.returncode == 1, done.stdout + done.stderr


def test_no_manifest_at_the_base_passes_and_says_why(repo, gate):
    (repo / _MANIFEST).unlink()
    assert _run(repo, "git", "commit", "-q", "-am", "drop").returncode == 0
    _write(repo, _BASE)
    done = gate()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "nothing to compare" in done.stdout


def test_deleting_the_manifest_fails(repo, gate):
    (repo / _MANIFEST).unlink()
    assert gate().returncode == 1
