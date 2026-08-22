from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from kiro_crew.crews.quality_engineering import (
    CREW_BLOCKED,
    CREW_COMPLETED,
    EvidenceRunResult,
    QualityAdapter,
    QualityCheck,
    QualityEngineeringCrew,
    QualityEvidenceRunner,
    load_agent_spec,
    load_quality_engineering_catalog,
    materialize_agent_specs,
)
from kiro_crew.crews.quality_engineering import package as quality_package
from kiro_crew.crews.quality_engineering.package import CrewPackageError, _payload_is_bounded


class _Context:
    def __init__(self, outputs: dict[str, dict]) -> None:
        self._run_id = "wf_test"
        self._session_key = "dashboard:test"
        self.now = "2026-08-20T00:00:00Z"
        self.outputs = outputs
        self.calls: list[dict] = []

    async def agent(self, prompt: str, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        for role_id, output in self.outputs.items():
            if f"Role: {role_id}" in prompt:
                return output
        raise AssertionError(f"unexpected role prompt: {prompt[:100]}")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("print('unchanged')\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def evidence_root(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("quality-evidence")


def test_catalog_and_private_agent_specs_are_report_only() -> None:
    catalog = load_quality_engineering_catalog()
    assert set(catalog.crews["quality-engineering"].routing) == {
        "qa_plan",
        "e2e_validation",
        "ux_review",
        "full_quality_review",
    }
    for role_id in ("qa-strategist", "e2e-engineer", "ux-reviewer"):
        spec = load_agent_spec(role_id)
        assert spec["name"].startswith("kirocrew-quality-engineering-")
        assert spec["includeMcpJson"] is False
        assert spec["tools"] == ["report"]
        assert spec["allowedTools"] == ["report"]


def test_materialization_is_collision_safe(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    prompts = tmp_path / "prompts"
    paths = materialize_agent_specs(agents, prompts)
    assert len(paths) == 3
    assert all(path.is_file() for path in paths)
    rendered = json.loads(paths[0].read_text(encoding="utf-8"))
    assert rendered["prompt"].startswith("file:")
    with pytest.raises(CrewPackageError, match="crew.materialize.exists"):
        materialize_agent_specs(agents, prompts)


@pytest.mark.asyncio
async def test_runner_uses_registered_argv_and_redacts_persisted_evidence(
    project: Path, evidence_root: Path, monkeypatch
) -> None:
    adapter = QualityAdapter("fake", "fake-executable")
    check = QualityCheck("fake-check", "fake", evidence_kind="application_e2e")
    runner = QualityEvidenceRunner(adapters={"fake": adapter}, checks={"fake-check": check})

    def fake_which(*_args, **_kwargs):
        return "/usr/bin/fake-executable"

    monkeypatch.setattr(quality_package.shutil, "which", fake_which)
    offloaded: list[object] = []
    audit_events: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kwargs):
            audit_events.append(kwargs)

    real_to_thread = quality_package.asyncio.to_thread

    async def tracked_to_thread(func, *args, **kwargs):
        offloaded.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(quality_package.asyncio, "to_thread", tracked_to_thread)
    monkeypatch.setattr(quality_package, "sel", lambda: _Sel())

    async def fake_execute(_argv, **_kwargs):
        fake_access_key = b"AKIA" + b"1234567890ABCDEF"
        return b"aws_access_key_id=" + fake_access_key, b"", 0, False, False

    monkeypatch.setattr(runner, "_execute_argv", fake_execute)
    results = await runner.run(
        project,
        ["fake-check"],
        evidence_root=evidence_root,
        session_key="dashboard:test",
    )

    assert results[0].status == "passed"
    assert results[0].evidence_path
    persisted = json.loads(Path(results[0].evidence_path).read_text(encoding="utf-8"))
    assert "AKIA" not in json.dumps(persisted)
    assert (project / "app.py").read_text(encoding="utf-8") == "print('unchanged')\n"

    second_runner = QualityEvidenceRunner(adapters={"fake": adapter}, checks={"fake-check": check})
    monkeypatch.setattr(second_runner, "_execute_argv", fake_execute)
    second_results = await second_runner.run(project, ["fake-check"], evidence_root=evidence_root)
    assert second_results[0].status == "passed"
    assert second_results[0].evidence_path != results[0].evidence_path
    assert any(getattr(func, "__name__", "") == "_copy_workspace" for func in offloaded)
    assert any(func is fake_which for func in offloaded)
    assert [event["outcome"] for event in audit_events] == ["invoked", "completed"]
    assert all(str(project) not in event["resources"] for event in audit_events)


@pytest.mark.asyncio
async def test_execute_argv_offloads_sandbox_argv(tmp_path, monkeypatch):
    runner = QualityEvidenceRunner()
    offloaded: list[object] = []
    real_to_thread = quality_package.asyncio.to_thread

    async def tracked_to_thread(func, *args, **kwargs):
        offloaded.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(quality_package.asyncio, "to_thread", tracked_to_thread)

    def fake_sandbox(argv, **_kwargs):
        return list(argv), {"PATH": "/usr/bin"}, ""

    monkeypatch.setattr(quality_package, "sandboxed_spawn_argv", fake_sandbox)

    class FakeProcess:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.returncode = None

        async def wait(self):
            self.returncode = 0
            return self.returncode

    process = FakeProcess()
    process.stdout.feed_data(b"ok\\n")
    process.stdout.feed_eof()
    process.stderr.feed_eof()

    async def fake_create_subprocess(*_args, **_kwargs):
        return process

    monkeypatch.setattr(quality_package, "create_subprocess_limited", fake_create_subprocess)

    stdout, stderr, returncode, timed_out, overflow = await runner._execute_argv(
        ("/bin/true",),
        cwd=tmp_path,
        env={"PATH": "/usr/bin"},
        timeout_seconds=5,
        max_output_bytes=100,
    )

    assert stdout == b"ok\\n"
    assert stderr == b""
    assert returncode == 0
    assert timed_out is False
    assert overflow is False
    assert offloaded == [fake_sandbox]


@pytest.mark.asyncio
async def test_runner_rejects_relative_paths_and_unknown_checks(
    project: Path, evidence_root: Path
) -> None:
    runner = QualityEvidenceRunner()
    with pytest.raises(CrewPackageError, match="crew.project_path"):
        await runner.run("relative/project", ["browser_e2e"], evidence_root=evidence_root)

    results = await runner.run(project, ["unknown-check"], evidence_root=evidence_root)
    assert results[0].status == "blocked"
    assert results[0].blocked_reason == "crew.check.unknown"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not __import__("kiro_crew.sandbox", fromlist=["userns_available"]).userns_available(),
    reason="requires unprivileged user namespaces (sandbox backend)",
)
async def test_execute_argv_completes_normally(project: Path) -> None:
    runner = QualityEvidenceRunner()
    stdout, stderr, code, timed_out, overflow = await runner._execute_argv(
        (sys.executable, "-c", "print('ok')"),
        cwd=project,
        env={"PATH": "", "HOME": str(project)},
        timeout_seconds=5,
        max_output_bytes=1000,
    )
    assert stdout.strip() == b"ok"
    assert stderr == b""
    assert code == 0
    assert timed_out is False
    assert overflow is False


@pytest.mark.asyncio
@pytest.mark.skipif(
    not __import__("kiro_crew.sandbox", fromlist=["userns_available"]).userns_available(),
    reason="requires unprivileged user namespaces (sandbox backend)",
)
async def test_execute_argv_times_out_and_caps_output(project: Path) -> None:
    runner = QualityEvidenceRunner()
    result = await runner._execute_argv(
        (sys.executable, "-c", "import time; time.sleep(2)"),
        cwd=project,
        env={"PATH": "", "HOME": str(project)},
        timeout_seconds=1,
        max_output_bytes=1000,
    )
    assert result[3] is True

    overflow = await runner._execute_argv(
        (sys.executable, "-c", "print('x' * 10000)"),
        cwd=project,
        env={"PATH": "", "HOME": str(project)},
        timeout_seconds=5,
        max_output_bytes=100,
    )
    assert overflow[4] is True
    assert len(overflow[0]) <= 100


@pytest.mark.asyncio
@pytest.mark.skipif(
    not __import__("kiro_crew.sandbox", fromlist=["userns_available"]).userns_available(),
    reason="requires unprivileged user namespaces (sandbox backend)",
)
async def test_execute_argv_cancellation_terminates_process(project: Path) -> None:
    runner = QualityEvidenceRunner()
    task = asyncio.create_task(
        runner._execute_argv(
            (sys.executable, "-c", "import time; time.sleep(10)"),
            cwd=project,
            env={"PATH": "", "HOME": str(project)},
            timeout_seconds=30,
            max_output_bytes=1000,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_payload_bounds_reject_nested_overflow() -> None:
    assert _payload_is_bounded({"nested": {"value": "ok"}})
    assert not _payload_is_bounded({"items": list(range(65))})
    assert not _payload_is_bounded({"text": "x" * 4_001})
    deep: object = "ok"
    for _ in range(9):
        deep = {"nested": deep}
    assert not _payload_is_bounded(deep)


@pytest.mark.asyncio
async def test_malformed_role_handoff_fails_closed(project: Path, evidence_root: Path) -> None:
    ctx = _Context({"qa-strategist": {"status": "passed"}})
    result = await QualityEngineeringCrew().run(
        ctx,
        request={
            "request": "Create a test plan",
            "project_path": str(project),
            "evidence_root": str(evidence_root),
        },
        route="qa_plan",
        workflow_id="wf_test",
    )
    assert result.status == CREW_BLOCKED
    assert result.blocked_reason == "crew.handoff.invalid:qa-strategist"


@pytest.mark.asyncio
async def test_qa_route_validates_role_handoff(project: Path, evidence_root: Path) -> None:
    ctx = _Context(
        {
            "qa-strategist": {
                "status": "passed",
                "scope": "changed app",
                "test_cases": ["happy path"],
                "risks": [],
                "required_evidence": [],
                "findings": [],
            }
        }
    )
    result = await QualityEngineeringCrew().run(
        ctx,
        request={
            "request": "Create a test plan",
            "project_path": str(project),
            "evidence_root": str(evidence_root),
        },
        route="qa_plan",
        workflow_id="wf_test",
    )
    assert result.status == CREW_COMPLETED
    assert result.handoffs[0].artifact_type == "qa_plan"
    assert len(ctx.calls) == 1


@pytest.mark.asyncio
async def test_full_route_fails_closed_for_capability_probe(
    project: Path, evidence_root: Path
) -> None:
    ctx = _Context({})
    result = await QualityEngineeringCrew().run(
        ctx,
        request={
            "request": "Run the full quality review",
            "project_path": str(project),
            "evidence_root": str(evidence_root),
        },
        route="full_quality_review",
        workflow_id="wf_test",
    )
    assert result.status == CREW_BLOCKED
    assert result.blocked_reason in {
        "crew.capability.unavailable",
        "crew.evidence.application_check_required",
    }
    assert ctx.calls == []


class _PassingRunner:
    async def run(self, *_args, **_kwargs):
        return (
            EvidenceRunResult(
                check_id="app-e2e",
                adapter_id="fake",
                status="passed",
                evidence_path="/tmp/evidence/app-e2e.json",
                evidence_kind="application_e2e",
            ),
        )


@pytest.mark.asyncio
async def test_full_route_adds_schema_validated_quality_report(
    project: Path, evidence_root: Path
) -> None:
    ctx = _Context(
        {
            "qa-strategist": {
                "status": "passed",
                "scope": "app",
                "test_cases": ["happy path"],
                "risks": [],
                "required_evidence": [],
                "findings": [],
            },
            "e2e-engineer": {
                "status": "passed",
                "checks": ["app-e2e"],
                "evidence_refs": ["/tmp/evidence/app-e2e.json"],
                "findings": [],
                "blocked_reason": "",
            },
            "ux-reviewer": {
                "status": "passed",
                "findings": [],
                "accessibility_checks": ["names present"],
                "usability_checks": ["keyboard flow"],
                "evidence_refs": ["/tmp/evidence/app-e2e.json"],
            },
        }
    )
    result = await QualityEngineeringCrew(runner=_PassingRunner()).run(
        ctx,
        request={
            "request": "Run the full quality review",
            "project_path": str(project),
            "check_ids": ["app-e2e"],
            "evidence_root": str(evidence_root),
        },
        route="full_quality_review",
        workflow_id="wf_test",
    )
    assert result.status == CREW_COMPLETED
    assert result.handoffs[-1].artifact_type == "quality_report"
    assert result.handoffs[-1].payload["status"] == "passed"
    assert len(result.handoffs) == 4
