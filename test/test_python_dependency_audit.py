"""The Python audit reports advisories and never decides a build.

The step this replaces was removed for being an unpinned network install with no
baseline, so the properties worth pinning are: the tool is invoked from the
pinned interpreter, a found advisory does not change the exit status, a tool that
did not run DOES, and a missing baseline is unmeasured rather than zero.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
_GATE_SCRIPT = ROOT / "scripts" / "check_python_audit.py"


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_python_audit_under_test", _GATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Match normal import semantics so dataclasses can resolve the module while executing.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


gate = _load_gate()


def _report(*entries: dict[str, Any]) -> str:
    return json.dumps({"dependencies": list(entries)})


def _dep(name: str, version: str, *advisories: str) -> dict[str, Any]:
    return {
        "name": name,
        "version": version,
        "vulns": [{"id": advisory} for advisory in advisories],
    }


def _completed(stdout: str, *, returncode: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "package": "example-package",
        "advisory": "GHSA-2345-6789-cfgh",
        "paths": ["website/package-lock.json"],
        "reason": "Mitigated while upgrading.",
        "owner": "@security-team",
        "expires": "2026-12-01",
    }
    entry.update(overrides)
    return entry


def _exceptions(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "exceptions": list(entries)}


class TestTheToolIsPinned:
    def test_the_audit_runs_through_the_pinned_interpreter(self) -> None:
        # A bare `pip-audit` would resolve against PATH, which is how a version
        # other than the pinned one ends up auditing.
        command = gate.audit_command()
        assert command[:3] == [sys.executable, "-m", "pip_audit"]
        assert "--format" in command and "json" in command

    def test_the_pinned_version_is_declared_in_the_dev_group(self) -> None:
        # The pin is what makes the audit reproducible, and it must live in the
        # group CI already installs rather than in a bespoke install step.
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        pin = re.search(r'"pip-audit==([0-9][0-9A-Za-z.+-]*)"', pyproject)
        assert pin is not None, "pip-audit must be pinned to an exact version"

    def test_no_workflow_installs_the_tool_unpinned(self) -> None:
        # Asserted against the workflows rather than by grepping pyproject for the
        # old command: that string legitimately appears in prose explaining why the
        # pin exists, so a text search there passes or fails on a comment.
        offenders: list[str] = []
        for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            body = workflow.read_text(encoding="utf-8")
            for line in body.splitlines():
                stripped = line.strip()
                # Comments are prose: this file and ci.yml both NAME the old
                # unpinned command to explain why the pin exists, so matching
                # comment text would fail on an explanation rather than a command.
                if stripped.startswith("#"):
                    continue
                if "pip-audit" not in stripped or "install" not in stripped:
                    continue
                if "pip-audit==" not in stripped:
                    offenders.append(f"{workflow.name}: {stripped}")
        assert offenders == []

    def test_ci_runs_the_audit_and_marks_it_advisory(self) -> None:
        # Pinned so neither half can drift silently: losing the step restores the
        # blind spot the issue is about, and losing continue-on-error turns a
        # report into a blocker without anyone deciding to.
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        step = re.search(
            r"- name: Python dependency audit[^\n]*\n(?P<body>(?:\s{8}[^\n]*\n)+)", workflow
        )
        assert step is not None, "ci.yml must run the Python audit"
        body = step.group("body")
        assert "continue-on-error: true" in body
        assert "scripts/check_python_audit.py" in body


class TestReportingNeverDecidesTheBuild:
    def test_advisories_do_not_change_the_exit_status(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(gate, "_REPO_ROOT", ROOT)
        monkeypatch.setattr(
            gate,
            "run_audit",
            lambda *_a, **_k: [
                gate.PythonFinding("example", "1.0", "GHSA-aaaa-bbbb-cccc"),
                gate.PythonFinding("other", "2.0", "PYSEC-2026-1"),
            ],
        )
        monkeypatch.setattr(gate, "load_baseline", lambda *_a, **_k: 0)
        assert gate.main() == 0
        out = capsys.readouterr().out
        assert "2 unexcepted" in out
        assert "ROSE above its baseline" in out

    def test_an_audit_that_could_not_run_is_the_one_failure(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # "Clean" and "did not run" must not look the same, or the gate reports
        # safety it never measured.
        def _refuse(*_a: Any, **_k: Any) -> list[Any]:
            raise gate.AuditError("interpreter has no pip_audit")

        monkeypatch.setattr(gate, "run_audit", _refuse)
        assert gate.main() == 1
        assert "could not run" in capsys.readouterr().err

    def test_a_count_at_or_below_baseline_prints_no_note(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(gate, "_REPO_ROOT", ROOT)
        monkeypatch.setattr(
            gate, "run_audit", lambda *_a, **_k: [gate.PythonFinding("a", "1", "PYSEC-1")]
        )
        monkeypatch.setattr(gate, "load_baseline", lambda *_a, **_k: 1)
        assert gate.main() == 0
        assert "ROSE above its baseline" not in capsys.readouterr().out


class TestParsingFailsLoudRatherThanEmpty:
    def test_findings_are_read_per_vulnerability(self) -> None:
        findings = gate.parse_report(_report(_dep("pkg", "1.2.3", "PYSEC-2026-1", "GHSA-x")))
        assert [f.identity for f in findings] == ["pkg 1.2.3 PYSEC-2026-1", "pkg 1.2.3 GHSA-x"]

    def test_a_clean_environment_is_no_findings(self) -> None:
        assert gate.parse_report(_report(_dep("pkg", "1.2.3"))) == []

    @pytest.mark.parametrize(
        "payload",
        [
            "not json",
            json.dumps({"no_dependencies": []}),
            json.dumps({"dependencies": "nope"}),
            json.dumps({"dependencies": ["scalar"]}),
            json.dumps({"dependencies": [{"name": "pkg"}]}),
            json.dumps({"dependencies": [{"name": "pkg", "version": "1", "vulns": [{}]}]}),
        ],
    )
    def test_unusable_output_raises_instead_of_counting_zero(self, payload: str) -> None:
        # A tool whose output shape changed would otherwise report clean forever.
        with pytest.raises(gate.AuditError):
            gate.parse_report(payload)

    def test_exit_one_is_a_successful_run_that_found_something(self) -> None:
        # pip-audit signals findings with exit 1; treating that as a tool failure
        # would make every vulnerable environment look like a broken audit.
        findings = gate.run_audit(
            runner=lambda *_a, **_k: _completed(_report(_dep("p", "1", "PYSEC-9")), returncode=1)
        )
        assert [f.advisory for f in findings] == ["PYSEC-9"]

    def test_an_unexpected_exit_status_is_a_failure(self) -> None:
        with pytest.raises(gate.AuditError):
            gate.run_audit(runner=lambda *_a, **_k: _completed("", returncode=2, stderr="boom"))

    def test_a_timeout_is_a_failure_not_a_clean_report(self) -> None:
        def _timeout(*_a: Any, **_k: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="pip-audit", timeout=1)

        with pytest.raises(gate.AuditError):
            gate.run_audit(runner=_timeout)


class TestBaselineAndExceptions:
    def test_a_recorded_count_is_read(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.json"
        path.write_text(
            json.dumps({"version": 1, "environments": {gate.ENVIRONMENT_KEY: 4}}),
            encoding="utf-8",
        )
        assert gate.load_baseline(path) == 4

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            "{not json",
            json.dumps({"version": 1}),
            json.dumps({"version": 1, "environments": {gate.ENVIRONMENT_KEY: -1}}),
            json.dumps({"version": 1, "environments": {gate.ENVIRONMENT_KEY: True}}),
            json.dumps({"version": 1, "environments": {gate.ENVIRONMENT_KEY: "3"}}),
        ],
    )
    def test_an_unusable_baseline_is_unmeasured_not_zero(
        self, tmp_path: Path, payload: str | None
    ) -> None:
        path = tmp_path / "baseline.json"
        if payload is not None:
            path.write_text(payload, encoding="utf-8")
        assert gate.load_baseline(path) is None

    def test_the_shipped_baseline_matches_the_documented_shape(self) -> None:
        document = json.loads((ROOT / gate.BASELINE_FILENAME).read_text(encoding="utf-8"))
        assert document["version"] == 1
        assert isinstance(document["environments"], dict)
        assert "never raise" in document["_comment"].lower()

    def test_regenerating_preserves_the_comment(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.json"
        path.write_text(
            json.dumps({"_comment": "keep me", "version": 1, "environments": {}}), encoding="utf-8"
        )
        gate.write_baseline(path, 3)
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["_comment"] == "keep me"
        assert written["environments"][gate.ENVIRONMENT_KEY] == 3

    def test_the_shared_exception_file_governs_python_advisories(self, tmp_path: Path) -> None:
        # One exception format for both ecosystems: a Python advisory carries the
        # same owner and expiry as a Node one instead of a second mechanism.
        path = tmp_path / "exceptions.json"
        path.write_text(json.dumps(_exceptions(_entry())), encoding="utf-8")
        assert gate.excepted_keys(path, today=date(2026, 8, 1)) == {
            ("example-package", "GHSA-2345-6789-cfgh")
        }

    def test_an_expired_exception_stops_excepting(self, tmp_path: Path) -> None:
        # The whole point of `expires` is that the entry stops applying. Reading
        # the file without it suppresses a finding forever.
        path = tmp_path / "exceptions.json"
        path.write_text(json.dumps(_exceptions(_entry(expires="2026-07-31"))), encoding="utf-8")
        assert gate.excepted_keys(path, today=date(2026, 8, 1)) == set()

    def test_an_exception_is_valid_through_its_expiry_date(self, tmp_path: Path) -> None:
        # Matches the npm gate's reading of the same field in the same file.
        path = tmp_path / "exceptions.json"
        path.write_text(json.dumps(_exceptions(_entry(expires="2026-08-01"))), encoding="utf-8")
        assert gate.excepted_keys(path, today=date(2026, 8, 1)) != set()

    def test_an_entry_only_excepts_its_own_package(self) -> None:
        # An entry filed for one package must not suppress the same advisory
        # against a different one.
        excepted = {("example-package", "GHSA-2345-6789-cfgh")}
        findings = [
            gate.PythonFinding("example-package", "1.0", "GHSA-2345-6789-cfgh"),
            gate.PythonFinding("other-package", "2.0", "GHSA-2345-6789-cfgh"),
        ]
        assert [f.package for f in gate.unexcepted(findings, excepted)] == ["other-package"]

    @pytest.mark.parametrize(
        "override",
        [
            {"package": ""},
            {"advisory": ""},
            {"expires": "not-a-date"},
            {"expires": 20260801},
        ],
    )
    def test_a_malformed_entry_excepts_nothing(
        self, tmp_path: Path, override: dict[str, Any]
    ) -> None:
        # A malformed file must except LESS, never more.
        path = tmp_path / "exceptions.json"
        path.write_text(json.dumps(_exceptions(_entry(**override))), encoding="utf-8")
        assert gate.excepted_keys(path, today=date(2026, 8, 1)) == set()

    def test_a_missing_exception_file_excepts_nothing(self, tmp_path: Path) -> None:
        assert gate.excepted_keys(tmp_path / "absent.json") == set()

    def test_the_repository_exception_file_is_the_one_the_npm_gate_uses(self) -> None:
        # Two gates, one governed list. A second filename would let an advisory be
        # excepted for one ecosystem and silently outstanding for the other.
        npm_gate = (ROOT / "scripts" / "check_npm_audit.py").read_text(encoding="utf-8")
        assert f'EXCEPTIONS_FILENAME = "{gate.EXCEPTIONS_FILENAME}"' in npm_gate
