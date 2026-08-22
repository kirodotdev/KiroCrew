from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import kiro_crew.crew_dispatch as dispatch
import kiro_crew.crew_registry as registry
import kiro_crew.crews.knowledge_quality as knowledge_crew
import kiro_crew.crews.knowledge_quality.package as knowledge_package
import kiro_crew.crews.software_delivery as software_crew
import kiro_crew.crews.software_delivery.package as software_package
from kiro_crew.crew_catalog import (
    CatalogValidationError,
    CrewCatalog,
    CrewRoute,
    load_catalog,
    parse_crew_definition,
    parse_role_definition,
    validate_crew_definition,
    validate_role_definition,
)
from kiro_crew.workflows.role_resolver import (
    ROLE_BLOCKED,
    ROLE_COMPLETED,
    RoleHandoff,
    RoleInvocation,
    RoleResolutionError,
)


def test_native_software_delivery_specs_do_not_bypass_tool_gate() -> None:
    specs_dir = (
        Path(__file__).parents[1]
        / "src"
        / "kiro_crew"
        / "crews"
        / "software_delivery"
        / "agent_specs"
    )
    for spec_path in sorted(specs_dir.glob("*.json")):
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        assert "allowedTools" not in spec, spec_path


def _role(
    role_id: str = "builder",
    *,
    side_effects: str = "none",
    input_schema: str = "input",
    output_schema: str = "output",
) -> dict[str, object]:
    return {
        "id": role_id,
        "version": "1.0.0",
        "mission": "Do the assigned work.",
        "agent": f"agent-{role_id}",
        "skills": ["skill"],
        "tool_scopes": ["read"],
        "profile": "read-only",
        "input_schema": input_schema,
        "output_schema": output_schema,
        "handoff": f"{role_id}-handoff",
        "quality_gates": ["checked"],
        "side_effects": side_effects,
    }


def _crew(
    roles: list[str] | None = None,
    *,
    route_roles: list[str] | None = None,
    approval: bool = False,
    target_write: str = "none",
    push: str = "none",
) -> dict[str, object]:
    declared = roles or ["builder"]
    return {
        "id": "example-crew",
        "version": "1.0.0",
        "roles": declared,
        "routing": {
            "default": {
                "roles": route_roles if route_roles is not None else declared,
                "approval": approval,
            }
        },
        "handoffs": [f"{role}-handoff" for role in declared],
        "policies": {"target_write": target_write, "push": push},
    }


def _catalog_document() -> dict[str, object]:
    role = _role()
    return {"schema": 1, "roles": [role], "crews": [_crew()]}


def _handoff(
    role_id: str,
    payload: object,
    *,
    workflow_id: str = "workflow-1",
    handoff_id: str = "handoff-1",
) -> RoleHandoff:
    return RoleHandoff(
        handoff_id=handoff_id,
        crew_id="crew",
        workflow_id=workflow_id,
        source_role=role_id,
        source_session="session",
        artifact_type=f"{role_id}-handoff",
        schema_version="1",
        payload=payload,
        created_at="now",
        quality_status="schema_validated",
    )


def _invocation(resolved, payload: object = None, *, blocked: bool = False) -> RoleInvocation:
    if blocked:
        return RoleInvocation(
            resolved=resolved,
            status=ROLE_BLOCKED,
            result=None,
            handoff=None,
            blocked_reason="stub_blocked",
        )
    return RoleInvocation(
        resolved=resolved,
        status=ROLE_COMPLETED,
        result=payload,
        handoff=_handoff(resolved.role.id, payload, workflow_id=resolved.workflow_id),
    )


def test_catalog_validates_parses_and_freezes_a_valid_document() -> None:
    raw = _catalog_document()
    assert validate_role_definition(raw["roles"][0]) == []  # type: ignore[index]
    assert validate_crew_definition(raw["crews"][0], {"builder": parse_role_definition(raw["roles"][0])}) == []  # type: ignore[index]

    role = parse_role_definition(raw["roles"][0])  # type: ignore[index]
    crew = parse_crew_definition(raw["crews"][0], {role.id: role})  # type: ignore[index]
    catalog = load_catalog(raw)

    assert role.id == "builder"
    assert crew.routing["default"].roles == ("builder",)
    assert catalog.roles["builder"] == role
    assert catalog.crews["example-crew"].policies["push"] == "none"


def test_catalog_reports_role_and_crew_shape_errors() -> None:
    bad_role = {
        "id": "Bad_ID",
        "version": "v1",
        "mission": " ",
        "agent": None,
        "skills": "not-a-list",
        "tool_scopes": ["read", ""],
        "profile": "",
        "input_schema": "",
        "output_schema": "output",
        "handoff": "handoff",
        "quality_gates": [],
        "side_effects": "unsafe",
    }
    errors = validate_role_definition(bad_role)
    assert "role.id.invalid" in errors
    assert "role.version.invalid" in errors
    assert "role.skills.not_list" in errors
    assert "role.tool_scopes[1].required" in errors
    assert "role.side_effects.invalid" in errors
    assert validate_role_definition(None) == ["role.not_object"]

    roles = {
        "builder": parse_role_definition(_role("builder", side_effects="candidate-write")),
        "approver": parse_role_definition(_role("approver", side_effects="approval-required")),
    }
    bad_crew = {
        "id": "Bad Crew",
        "version": "v1",
        "roles": ["builder", "approver", "builder", "missing-role"],
        "routing": {
            "": {"roles": [], "approval": "yes"},
            "broken": "not-an-object",
            "default": {
                "roles": ["builder", "approver", "builder", "undeclared"],
                "approval": False,
            },
        },
        "handoffs": ["handoff", "handoff"],
        "policies": {"target_write": "none", "push": "unsafe"},
    }
    errors = validate_crew_definition(bad_crew, roles)
    assert "crew.id.invalid" in errors
    assert "crew.roles.duplicate:builder" in errors
    assert "crew.roles.unknown:missing-role" in errors
    assert "crew.routing.name.required" in errors
    assert "crew.routing.broken.not_object" in errors
    assert "crew.routing.default.roles.not_declared:undeclared" in errors
    assert "crew.routing.default.approval.required_for:approver" in errors
    assert "crew.policies.target_write.insufficient" in errors
    assert "crew.policies.target_write.approval_required" in errors
    assert validate_crew_definition(None) == ["crew.not_object"]


def test_catalog_loader_rejects_shapes_duplicates_and_bad_schema() -> None:
    with pytest.raises(CatalogValidationError, match="catalog.not_object"):
        load_catalog(None)
    with pytest.raises(CatalogValidationError, match="catalog.schema.unsupported"):
        load_catalog({"schema": True, "roles": [], "crews": []})
    with pytest.raises(CatalogValidationError, match="catalog.roles.not_list"):
        load_catalog({"schema": 1, "roles": "bad", "crews": "bad"})

    role = _role()
    duplicate_role = {"schema": 1, "roles": [role, role], "crews": []}
    with pytest.raises(CatalogValidationError, match="role.id.duplicate:builder"):
        load_catalog(duplicate_role)

    document = _catalog_document()
    document["crews"] = [document["crews"][0], document["crews"][0]]  # type: ignore[index]
    with pytest.raises(CatalogValidationError, match="crew.id.duplicate:example-crew"):
        load_catalog(document)

    malformed = {"schema": 1, "roles": [{"id": "bad"}], "crews": [{"id": "bad"}]}
    with pytest.raises(CatalogValidationError, match=r"roles\[0\]"):
        load_catalog(malformed)


def test_dispatch_bounds_payloads_builds_requests_and_normalizes_results(
    tmp_path: Path, monkeypatch
) -> None:
    nested: object = "leaf"
    for _ in range(10):
        nested = [nested]
    assert dispatch._redact_bounded({"nested": nested, "object": object(), "ok": True})["nested"]
    assert dispatch._redact_bounded((1, "text")) == [1, "text"]
    assert dispatch._redact_bounded(None) is None

    class Result:
        def to_dict(self):
            return {"payload": {"secret": "value"}, "status": "completed"}

    assert dispatch._result_dict(Result())["status"] == "completed"
    assert dispatch._result_dict({"status": "blocked"})["status"] == "blocked"
    assert dispatch._result_dict(object())["blocked_reason"] == "crew.result.invalid"
    assert (
        dispatch._result_dict(type("BadResult", (), {"to_dict": lambda self: []})())["status"]
        == "blocked"
    )

    monkeypatch.setattr(dispatch, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(knowledge_package, "load_audit_cases", lambda: [{"id": "case-1"}])
    audit = dispatch._default_knowledge_request({"limit": 999})
    assert audit["limit"] == 20
    assert Path(audit["database_path"]).as_posix().endswith("workspace/knowledge/knowledge.db")
    assert dispatch._default_knowledge_request({"limit": True})["limit"] == 5

    software = dispatch._software_request(
        {
            "request": "x" * 3000,
            "candidate_workspace": 42,
            "constraints": ["one", 2, "two"],
        }
    )
    assert software["candidate_workspace"] == ""
    assert len(software["request"]) <= dispatch._MAX_TEXT
    assert software["constraints"] == ["one", "two"]
    assert dispatch._software_request({"request": None, "constraints": "bad"}) == {
        "request": "",
        "candidate_workspace": "",
    }


@pytest.mark.asyncio
async def test_native_dispatch_selects_only_private_packages(monkeypatch) -> None:
    class Context:
        _run_id = "run-42"
        _session_key = "dashboard:slot"

    class FakeSoftwareCrew:
        async def run(self, ctx, **kwargs):
            assert ctx is context
            assert kwargs["workflow_id"] == "run-42"
            return {"status": "completed", "route": kwargs["route"]}

    class FakeKnowledgeCrew:
        async def run(self, ctx, **kwargs):
            assert ctx is context
            assert kwargs["workflow_id"] == "run-42"
            return {"status": "completed", "model": kwargs["model"]}

    context = Context()
    monkeypatch.setattr(software_crew, "SoftwareDeliveryCrew", FakeSoftwareCrew)
    monkeypatch.setattr(knowledge_crew, "KnowledgeQualityCrew", FakeKnowledgeCrew)

    assert await dispatch.execute_native_crew(context, "unknown", None) == {
        "status": "blocked",
        "blocked_reason": "crew.workflow.unknown",
    }
    software = await dispatch.execute_native_crew(
        context,
        dispatch.INTERNAL_SOFTWARE_WORKFLOW,
        {"route": "small_change", "request": "Fix it", "candidate_workspace": "/tmp/project"},
    )
    assert software["route"] == "small_change"
    knowledge = await dispatch.execute_native_crew(
        context,
        dispatch.INTERNAL_KNOWLEDGE_WORKFLOW,
        {"route": "retrieval_audit", "model": ""},
    )
    assert knowledge["model"] is None


def test_registry_lookup_and_materialization_state(tmp_path: Path, monkeypatch) -> None:
    assert registry.internal_crew_agent_names() == registry.INTERNAL_CREW_AGENT_NAMES

    class Package:
        AGENT_SPEC_FILES = {"role": "role.json"}
        _PROMPT_FILES = {"role": "agent.txt"}

        @staticmethod
        def load_agent_spec(role_id):
            return {"name": "agent-name", "includeMcpJson": False}

    agents = tmp_path / "agents"
    prompts = tmp_path / "prompts"
    assert not registry._package_is_materialized(Package, agents, prompts)
    agents.mkdir()
    prompts.mkdir()
    (agents / "agent-name.json").write_text("not-json", encoding="utf-8")
    (prompts / "agent.txt").write_text("prompt", encoding="utf-8")
    assert not registry._package_is_materialized(Package, agents, prompts)
    (agents / "agent-name.json").write_text(
        json.dumps({"name": "agent-name", "includeMcpJson": False}), encoding="utf-8"
    )
    assert registry._package_is_materialized(Package, agents, prompts) is False
    (agents / "agent-name.json").write_text(
        json.dumps(
            {
                "name": "agent-name",
                "includeMcpJson": False,
                "prompt": (prompts / "agent.txt").resolve().as_uri(),
            }
        ),
        encoding="utf-8",
    )
    assert registry._package_is_materialized(Package, agents, prompts)

    class InvalidPackage(Package):
        @staticmethod
        def load_agent_spec(role_id):
            return {"name": None, "includeMcpJson": False}

    assert not registry._package_is_materialized(InvalidPackage, agents, prompts)

    class MaterializingPackage(Package):
        AGENT_SPEC_FILES = {"role-two": "role-two.json"}
        _PROMPT_FILES = {"role-two": "agent-two.txt"}

        @staticmethod
        def load_agent_spec(role_id):
            return {"name": "agent-two", "includeMcpJson": False}

        @staticmethod
        def materialize_agent_specs(agents_dir, prompt_dir):
            return (agents_dir / "new.json",)

    class ErrorPackage(Package):
        @staticmethod
        def load_agent_spec(role_id):
            raise RuntimeError("broken")

    packages = {
        "first": Package,
        "second": MaterializingPackage,
        "third": ErrorPackage,
    }
    monkeypatch.setattr(registry, "_CREW_PACKAGE_MODULES", tuple(packages))
    monkeypatch.setattr(registry.importlib, "import_module", packages.__getitem__)
    result = registry.materialize_builtin_crew_agents(agents, prompts)
    assert result == (agents.resolve() / "new.json",)


def test_software_package_resources_materialization_and_helpers(
    tmp_path: Path, monkeypatch
) -> None:
    catalog = software_package.load_software_delivery_catalog()
    assert catalog.crews["software-delivery"].id == "software-delivery"
    for role_id in software_package.AGENT_SPEC_FILES:
        spec = software_package.load_agent_spec(role_id)
        assert spec["name"].startswith("kirocrew-software-delivery-")
    with pytest.raises(software_package.CrewPackageError, match="crew.agent_role.unknown"):
        software_package.load_agent_spec("missing")

    with pytest.raises(software_package.CrewPackageError, match="invalid_json"):
        monkeypatch.setattr(software_package, "_resource_text", lambda *_: "{")
        software_package._resource_json("broken.json")
    monkeypatch.setattr(software_package, "_resource_text", lambda *_: "[]")
    with pytest.raises(software_package.CrewPackageError, match="not_object"):
        software_package._resource_json("array.json")
    monkeypatch.undo()

    class BrokenResource:
        def joinpath(self, part):
            return self

        def read_text(self, encoding):
            raise OSError("unavailable")

    monkeypatch.setattr(software_package.resources, "files", lambda _: BrokenResource())
    with pytest.raises(software_package.CrewPackageError, match="resource.unavailable"):
        software_package._resource_text("missing.txt")
    monkeypatch.undo()

    with pytest.raises(software_package.CrewPackageError, match="absolute"):
        software_package._resolved_target(Path("relative"), "target.invalid")
    file_target = tmp_path / "file"
    file_target.write_text("x", encoding="utf-8")
    with pytest.raises(software_package.CrewPackageError, match="target.invalid"):
        software_package._resolved_target(file_target, "target.invalid")
    monkeypatch.setattr(software_package, "is_sensitive_path", lambda _: True)
    with pytest.raises(software_package.CrewPackageError, match="sensitive_target"):
        software_package._resolved_target(tmp_path / "safe", "target.invalid")
    monkeypatch.undo()

    agents = tmp_path / "agents"
    prompts = tmp_path / "prompts"
    paths = software_package.materialize_agent_specs(agents, prompts)
    assert len(paths) == 4
    assert all(path.is_file() for path in paths)
    with pytest.raises(software_package.CrewPackageError, match="exists"):
        software_package.materialize_agent_specs(agents, prompts)
    with pytest.raises(software_package.CrewPackageError, match="overwrite_unsupported"):
        software_package.materialize_agent_specs(
            tmp_path / "new-agents", tmp_path / "new-prompts", overwrite=True
        )

    request = {
        "request": "fix",
        "constraints": ["safe"],
        "acceptance_criteria": ["tested"],
        "candidate_workspace": str(tmp_path),
    }
    handoffs = {
        "architecture_brief": {"decision": "use it"},
        "implementation_result": {"changed_paths": ["a.py"]},
        "validation_report": {"status": "passed"},
    }
    assert software_package._role_input("software-engineer", request, handoffs)[
        "architecture_brief"
    ]
    assert software_package._role_input("validator", request, handoffs)["implementation_result"]
    security_input = software_package._role_input(
        "security-reliability-reviewer", request, handoffs
    )
    assert security_input["changed_paths"] == ["a.py"]
    assert security_input["validation_report"]["status"] == "passed"
    assert software_package._candidate_workspace({"candidate_workspace": str(tmp_path)}) == str(
        tmp_path.resolve()
    )
    with pytest.raises(software_package.CrewPackageError, match="candidate_workspace.invalid"):
        software_package._candidate_workspace({"candidate_workspace": "relative"})
    monkeypatch.setattr(software_package, "is_sensitive_path", lambda _: True)
    with pytest.raises(software_package.CrewPackageError, match="candidate_workspace.sensitive"):
        software_package._candidate_workspace({"candidate_workspace": str(tmp_path)})
    monkeypatch.undo()
    assert software_package._required_text(" value ", "required") == "value"
    with pytest.raises(software_package.CrewPackageError, match="required"):
        software_package._required_text(" ", "required")


def _software_payload(role_id: str) -> dict[str, object]:
    if role_id == "software-engineer":
        return {"changed_paths": ["src/app.py"], "tests": ["pytest"], "limitations": []}
    if role_id == "validator":
        return {"status": "passed", "checks": ["pytest"], "failures": []}
    if role_id == "solution-architect":
        return {
            "request": "change",
            "problem": "problem",
            "goals": ["goal"],
            "non_goals": [],
            "constraints": [],
            "options": ["option"],
            "decision": "decision",
            "affected_components": ["component"],
            "acceptance_criteria": ["criterion"],
            "risks": [],
            "rollback_plan": "rollback",
        }
    return {"status": "passed", "findings": [], "residual_risk": "low"}


@pytest.mark.asyncio
async def test_software_delivery_run_success_approval_and_fail_closed_paths(
    tmp_path: Path, monkeypatch
) -> None:
    crew = software_package.SoftwareDeliveryCrew()

    async def execute(ctx, resolved, **kwargs):
        return _invocation(resolved, _software_payload(resolved.role.id))

    monkeypatch.setattr(software_package, "execute_role", execute)
    request = {"request": "change", "candidate_workspace": str(tmp_path)}
    result = await crew.run(
        SimpleNamespace(), request=request, route="small_change", workflow_id="wf"
    )
    assert result.status == software_package.CREW_COMPLETED
    assert len(result.handoffs) == 2
    assert result.to_dict()["status"] == "completed"

    context = SimpleNamespace(approve=AsyncMock(return_value=True))
    production = await crew.run(
        context,
        request=request,
        route="production_change",
        workflow_id="wf-production",
        approval_prompt="Approve now",
    )
    assert production.status == software_package.CREW_COMPLETED
    assert production.approval_granted is True
    context.approve.assert_awaited_once_with("Approve now")

    invalid = await crew.run(
        SimpleNamespace(),
        request={"request": "missing workspace"},
        route="small_change",
        workflow_id="wf",
    )
    assert invalid.blocked_reason == "crew.role.input_invalid:software-engineer"
    unavailable = await crew.run(
        SimpleNamespace(), request=request, route="production_change", workflow_id="wf"
    )
    assert unavailable.blocked_reason == "crew.approval.unavailable"
    rejected_ctx = SimpleNamespace(approve=AsyncMock(return_value=False))
    rejected = await crew.run(
        rejected_ctx, request=request, route="production_change", workflow_id="wf"
    )
    assert rejected.blocked_reason == "crew.approval.rejected"
    failed_ctx = SimpleNamespace(approve=AsyncMock(side_effect=RuntimeError("approval down")))
    failed = await crew.run(
        failed_ctx, request=request, route="production_change", workflow_id="wf"
    )
    assert failed.blocked_reason == "crew.approval.failed"
    invalid_ctx = SimpleNamespace(approve=AsyncMock(return_value="yes"))
    invalid_approval = await crew.run(
        invalid_ctx, request=request, route="production_change", workflow_id="wf"
    )
    assert invalid_approval.blocked_reason == "crew.approval.invalid"

    with pytest.raises(software_package.CrewPackageError, match="workflow_id.required"):
        await crew.run(SimpleNamespace(), request=request, route="small_change", workflow_id=" ")
    with pytest.raises(software_package.CrewPackageError, match="crew.route.required"):
        await crew.run(SimpleNamespace(), request=request, route=" ", workflow_id="wf")
    with pytest.raises(software_package.CrewPackageError, match="crew.request.not_object"):
        await crew.run(SimpleNamespace(), request=None, route="small_change", workflow_id="wf")
    with pytest.raises(software_package.CrewPackageError, match="crew.route.unknown"):
        await crew.run(SimpleNamespace(), request=request, route="unknown", workflow_id="wf")


@pytest.mark.asyncio
async def test_software_delivery_run_blocks_unknown_roles_resolution_and_handoffs(
    tmp_path: Path, monkeypatch
) -> None:
    base = software_package.load_software_delivery_catalog()
    definition = base.crews["software-delivery"]
    routes = dict(definition.routing)
    routes["two_roles"] = CrewRoute("two_roles", ("software-engineer", "missing"), False)
    custom = CrewCatalog(
        base.schema,
        base.roles,
        MappingProxyType(
            {"software-delivery": replace(definition, routing=MappingProxyType(routes))}
        ),
    )
    crew = software_package.SoftwareDeliveryCrew(custom)
    request = {"request": "change", "candidate_workspace": str(tmp_path)}

    async def success(ctx, resolved, **kwargs):
        return _invocation(resolved, _software_payload(resolved.role.id))

    monkeypatch.setattr(software_package, "execute_role", success)
    unknown = await crew.run(
        SimpleNamespace(), request=request, route="two_roles", workflow_id="wf"
    )
    assert "crew.role.unknown:missing" == unknown.blocked_reason

    def fail_resolve(*args, **kwargs):
        raise RoleResolutionError("schema_missing")

    monkeypatch.setattr(software_package, "resolve_role", fail_resolve)
    resolved = await crew.run(
        SimpleNamespace(), request=request, route="small_change", workflow_id="wf"
    )
    assert resolved.blocked_reason.endswith(":schema_missing")
    monkeypatch.undo()

    blocked = await crew.run(
        SimpleNamespace(),
        request={"request": "x", "candidate_workspace": "relative"},
        route="small_change",
        workflow_id="wf",
    )
    assert "candidate_workspace.invalid" in blocked.blocked_reason

    async def blocked_execute(ctx, resolved, **kwargs):
        return _invocation(resolved, blocked=True)

    monkeypatch.setattr(software_package, "execute_role", blocked_execute)
    blocked_result = await crew.run(
        SimpleNamespace(), request=request, route="small_change", workflow_id="wf"
    )
    assert "crew.role.blocked:software-engineer:stub_blocked" == blocked_result.blocked_reason

    async def bad_output(ctx, resolved, **kwargs):
        return _invocation(resolved, {"wrong": True})

    monkeypatch.setattr(software_package, "execute_role", bad_output)
    output = await crew.run(
        SimpleNamespace(), request=request, route="small_change", workflow_id="wf"
    )
    assert output.blocked_reason == "crew.handoff.invalid:software-engineer"

    routes = dict(definition.routing)
    routes["bad_second"] = CrewRoute("bad_second", ("software-engineer", "validator"), False)
    custom = CrewCatalog(
        base.schema,
        base.roles,
        MappingProxyType(
            {"software-delivery": replace(definition, routing=MappingProxyType(routes))}
        ),
    )
    crew = software_package.SoftwareDeliveryCrew(custom)
    original_validate = software_package.validate_against_schema
    calls = 0

    def invalid_second(payload, schema):
        nonlocal calls
        calls += 1
        return [] if calls < 4 else ["invalid"]

    monkeypatch.setattr(software_package, "validate_against_schema", invalid_second)
    monkeypatch.setattr(software_package, "execute_role", success)
    second = await crew.run(
        SimpleNamespace(), request=request, route="bad_second", workflow_id="wf"
    )
    assert second.blocked_reason == "crew.role.input_invalid:validator"
    assert original_validate is not None


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "knowledge.db"
    connection = sqlite3.connect(path)
    connection.execute("create table if not exists items (id integer)")
    connection.commit()
    connection.close()
    return path


def _case(case_id: str = "case-1") -> dict[str, object]:
    return {
        "id": case_id,
        "query": "where is the design?",
        "language": "en",
        "expected_source_uris": ["file:///design.md"],
        "expected_verdict": "pass",
    }


def _audit_request(path: Path) -> dict[str, object]:
    return {"database_path": str(path), "cases": [_case()], "limit": 3, "embedding_mode": "none"}


def test_knowledge_package_resources_validation_and_materialization(
    tmp_path: Path, monkeypatch
) -> None:
    catalog = knowledge_package.load_knowledge_quality_catalog()
    assert catalog.crews["knowledge-quality"].id == "knowledge-quality"
    assert knowledge_package.load_audit_cases()
    for role_id in knowledge_package.AGENT_SPEC_FILES:
        assert knowledge_package.load_agent_spec(role_id)["allowedTools"] == ["report"]
    with pytest.raises(knowledge_package.CrewPackageError, match="crew.agent_role.unknown"):
        knowledge_package.load_agent_spec("missing")

    with pytest.raises(knowledge_package.CrewPackageError, match="invalid_json"):
        monkeypatch.setattr(knowledge_package, "_resource_text", lambda *_: "{")
        knowledge_package._resource_value("broken.json")
    monkeypatch.setattr(knowledge_package, "_resource_text", lambda *_: "{}")
    with pytest.raises(knowledge_package.CrewPackageError, match="audit_cases.not_list"):
        knowledge_package.load_audit_cases()
    monkeypatch.setattr(knowledge_package, "_resource_value", lambda *_: [])
    with pytest.raises(knowledge_package.CrewPackageError, match="resource.not_object"):
        knowledge_package.load_knowledge_quality_catalog()
    monkeypatch.setattr(knowledge_package, "_resource_value", lambda *_: ["bad"])
    with pytest.raises(knowledge_package.CrewPackageError, match="audit_cases.invalid_case"):
        knowledge_package.load_audit_cases()
    monkeypatch.undo()

    agents = tmp_path / "agents"
    prompts = tmp_path / "prompts"
    paths = knowledge_package.materialize_agent_specs(agents, prompts)
    assert len(paths) == 3
    assert all(path.is_file() for path in paths)
    with pytest.raises(knowledge_package.CrewPackageError, match="exists"):
        knowledge_package.materialize_agent_specs(agents, prompts)
    with pytest.raises(knowledge_package.CrewPackageError, match="overwrite_unsupported"):
        knowledge_package.materialize_agent_specs(tmp_path / "a2", tmp_path / "p2", overwrite=True)


def test_knowledge_package_validation_observations_and_payload_helpers(
    tmp_path: Path, monkeypatch
) -> None:
    path = _database(tmp_path)
    assert knowledge_package._validate_database_path(str(path)) == path.resolve()
    for value, code in [
        (None, "required"),
        ("relative.db", "not_absolute"),
        (str(tmp_path / "missing.db"), "missing"),
    ]:
        with pytest.raises(knowledge_package.CrewPackageError, match=code):
            knowledge_package._validate_database_path(value)
    wal = path.with_name(path.name + "-wal")
    wal.write_text("active", encoding="utf-8")
    with pytest.raises(knowledge_package.CrewPackageError, match="active_wal"):
        knowledge_package._validate_database_path(str(path))
    wal.unlink()
    monkeypatch.setattr(knowledge_package, "is_sensitive_path", lambda _: True)
    with pytest.raises(knowledge_package.CrewPackageError, match="sensitive"):
        knowledge_package._validate_database_path(str(path))
    monkeypatch.undo()

    valid_cases = (_case(),)
    knowledge_package._validate_cases(valid_cases)
    invalid_cases = [
        ((), "empty"),
        (tuple([_case(str(i)) for i in range(knowledge_package._MAX_CASES + 1)]), "too_many"),
        (({**_case(), "id": ""},), "id.required"),
        (({**_case(), "id": "case-1"}, _case("case-1")), "duplicate"),
        (({**_case(), "query": 1},), "query.invalid"),
        (({**_case(), "language": "x" * 40},), "language.too_long"),
        (({**_case(), "expected_source_uris": "bad"},), "sources.invalid"),
        (({**_case(), "expected_source_uris": ["x" * 1100]},), "source.too_long"),
        (({**_case(), "expected_verdict": "unknown"},), "verdict.invalid"),
    ]
    for cases, code in invalid_cases:
        with pytest.raises(knowledge_package.CrewPackageError, match=code):
            knowledge_package._validate_cases(cases)

    request = _audit_request(path)
    assert knowledge_package._validate_request(request).limit == 3
    for bad, code in [
        ({**request, "cases": "bad"}, "audit_cases.not_list"),
        ({**request, "limit": True}, "audit.limit.invalid"),
        ({**request, "limit": 0}, "audit.limit.invalid"),
        ({**request, "embedding_mode": "bad"}, "embedding_mode.invalid"),
    ]:
        with pytest.raises(knowledge_package.CrewPackageError, match=code):
            knowledge_package._validate_request(bad)

    too_deep: object = "x"
    for _ in range(knowledge_package._MAX_PAYLOAD_DEPTH + 2):
        too_deep = [too_deep]
    assert not knowledge_package._payload_is_bounded(too_deep)
    assert not knowledge_package._payload_is_bounded({"x": object()})
    assert not knowledge_package._payload_is_bounded(
        {str(i): i for i in range(knowledge_package._MAX_PAYLOAD_KEYS + 1)}
    )
    assert not knowledge_package._payload_is_bounded(
        ["x"] * (knowledge_package._MAX_PAYLOAD_ITEMS + 1)
    )
    assert not knowledge_package._payload_is_bounded(float("nan"))
    assert knowledge_package._payload_is_bounded({"ok": [1, True, None]})
    redacted = knowledge_package._redact_payload({"text": "x", "items": [1, object()]})
    assert redacted["items"][0] == 1
    assert knowledge_package._redact_payload(too_deep)
    assert knowledge_package._handoff_text(123) == "123"
    assert knowledge_package._preview(None) == ""

    class Retriever:
        def search(self, query, limit):
            assert query == "query"
            assert limit == 2
            return [
                {
                    "source_uri": "file:///a",
                    "title": "Title",
                    "source_type": "note",
                    "source_name": "a",
                    "file_path": "/tmp/a",
                    "artifact_slug": "slug",
                    "section_title": "section",
                    "chunk_range": "1-2",
                    "match_type": "vector",
                    "score": 0.5,
                    "content": "content",
                }
            ]

    observed = knowledge_package._observation_for_case(
        Retriever(), {**_case(), "query": "query"}, 2
    )
    assert observed["outcome"] == "results"
    assert observed["observed_source_uris"] == ["file:///a"]

    class EmptyRetriever:
        def search(self, query, limit):
            return []

    assert (
        knowledge_package._observation_for_case(EmptyRetriever(), _case(), 2)["outcome"]
        == "no_results"
    )

    class FailingRetriever:
        def search(self, query, limit):
            raise RuntimeError("search failed")

    error = knowledge_package._observation_for_case(FailingRetriever(), _case(), 2)
    assert error["failure_class"] == "retrieval"
    assert error["error_type"] == "RuntimeError"


def test_knowledge_package_embedding_collection_and_role_payloads(
    tmp_path: Path, monkeypatch
) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir()
    monkeypatch.setattr(knowledge_package, "config_dir", lambda: config_root)

    class Embedder:
        def __init__(self, available):
            self.available = available

        def is_available(self):
            return self.available

        def embed(self, value):
            return [value]

    monkeypatch.setattr(
        knowledge_package, "create_embedder_from_config", lambda config: Embedder(True)
    )
    embed, available = knowledge_package._configured_embedder()
    assert callable(embed) and available
    monkeypatch.setattr(
        knowledge_package, "create_embedder_from_config", lambda config: Embedder(False)
    )
    assert knowledge_package._configured_embedder() == (None, False)
    monkeypatch.setattr(
        knowledge_package,
        "create_embedder_from_config",
        lambda config: (_ for _ in ()).throw(RuntimeError("bad config")),
    )
    assert knowledge_package._configured_embedder() == (None, False)

    class Db:
        def close(self):
            self.closed = True

    class Store:
        def __init__(self, path, read_only):
            assert read_only is True
            self.db = Db()

    class Retriever:
        def __init__(self, store, embedder):
            assert embedder is None

        def search(self, query, limit):
            return []

    monkeypatch.setattr(knowledge_package, "KnowledgeStore", Store)
    monkeypatch.setattr(knowledge_package, "HybridRetriever", Retriever)
    audit = knowledge_package._validate_request(_audit_request(_database(tmp_path)))
    observations, runtime = knowledge_package._collect_observations(audit)
    assert observations[0]["outcome"] == "no_results"
    assert runtime["read_only"] is True

    handoff = _handoff("researcher", {"claim": "value"})
    scope = {
        "case_count": 1,
        "case_ids": ["case-1"],
        "languages": ["en"],
        "result_limit": 2,
        "embedding_mode": "none",
    }
    observations = [{"case_id": "case-1"}]
    runtime = {"read_only": True}
    assert knowledge_package._scope(audit)["case_count"] == 1
    assert knowledge_package._handoff_envelope(handoff)["payload"]["claim"] == "value"
    assert (
        knowledge_package._role_payload(
            "retrieval-researcher",
            scope=scope,
            observations=observations,
            runtime=runtime,
            handoffs={},
        )["scope"]
        == scope
    )
    assert (
        knowledge_package._role_payload(
            "retrieval-validator",
            scope=scope,
            observations=observations,
            runtime=runtime,
            handoffs={"knowledge_audit_report": handoff},
        )["knowledge_audit_report"]["source_role"]
        == "researcher"
    )
    assert (
        knowledge_package._role_payload(
            "security-reliability-reviewer",
            scope=scope,
            observations=observations,
            runtime=runtime,
            handoffs={"validation_report": handoff},
        )["validation_report"]["source_role"]
        == "researcher"
    )
    with pytest.raises(knowledge_package.CrewPackageError, match="crew.role.unknown"):
        knowledge_package._role_payload(
            "unknown", scope=scope, observations=[], runtime={}, handoffs={}
        )


def _knowledge_payload(role_id: str) -> dict[str, object]:
    if role_id == "retrieval-researcher":
        return {
            "scope": {},
            "pipeline_observations": [],
            "case_results": [],
            "retrieval_findings": [],
            "migration_rollback_risks": [],
            "unverified_claims": [],
            "next_actions": [],
        }
    if role_id == "retrieval-validator":
        return {"status": "passed", "case_results": [], "evidence_gaps": [], "failures": []}
    return {"status": "passed", "findings": [], "residual_risk": "low"}


@pytest.mark.asyncio
async def test_knowledge_quality_run_success_override_and_fail_closed_paths(
    tmp_path: Path, monkeypatch
) -> None:
    crew = knowledge_package.KnowledgeQualityCrew()
    path = _database(tmp_path)
    request = _audit_request(path)
    monkeypatch.setattr(
        knowledge_package,
        "_collect_observations",
        lambda audit: ([{"case_id": "case-1"}], {"read_only": True}),
    )
    offloaded: list[object] = []
    real_to_thread = knowledge_package.asyncio.to_thread

    async def tracked_to_thread(func, *args, **kwargs):
        offloaded.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(knowledge_package.asyncio, "to_thread", tracked_to_thread)

    async def execute(ctx, resolved, **kwargs):
        return _invocation(resolved, _knowledge_payload(resolved.role.id))

    monkeypatch.setattr(knowledge_package, "execute_role", execute)
    result = await crew.run(
        SimpleNamespace(),
        request=request,
        route="retrieval_audit_with_risk_review",
        workflow_id="wf",
        model="configured-model",
    )
    assert result.status == knowledge_package.CREW_COMPLETED
    assert result.model_mode == "runtime_override"
    assert result.to_dict()["model_mode"] == "runtime_override"
    assert offloaded == [knowledge_package._collect_observations]

    invalid_model = await crew.run(
        SimpleNamespace(), request=request, route="retrieval_audit", workflow_id="wf", model=" "
    )
    assert invalid_model.blocked_reason == "crew.model.invalid"
    unknown = await crew.run(SimpleNamespace(), request=request, route="unknown", workflow_id="wf")
    assert unknown.blocked_reason == "crew.route.unknown"
    non_object = await crew.run(
        SimpleNamespace(), request=None, route="retrieval_audit", workflow_id="wf"
    )
    assert non_object.blocked_reason == "crew.request.not_object"
    bad_request = await crew.run(
        SimpleNamespace(),
        request={**request, "database_path": "missing"},
        route="retrieval_audit",
        workflow_id="wf",
    )
    assert bad_request.blocked_reason == "crew.database_path.not_absolute"
    monkeypatch.setattr(
        knowledge_package,
        "_collect_observations",
        lambda audit: (_ for _ in ()).throw(RuntimeError("open failed")),
    )
    preflight = await crew.run(
        SimpleNamespace(), request=request, route="retrieval_audit", workflow_id="wf"
    )
    assert preflight.blocked_reason == "crew.database.read_only_open_failed:RuntimeError"


@pytest.mark.asyncio
async def test_knowledge_quality_run_blocks_role_failures_and_bounds_handoffs(
    tmp_path: Path, monkeypatch
) -> None:
    base = knowledge_package.load_knowledge_quality_catalog()
    definition = base.crews["knowledge-quality"]
    routes = dict(definition.routing)
    routes["two_roles"] = CrewRoute("two_roles", ("retrieval-researcher", "missing"), False)
    custom = CrewCatalog(
        base.schema,
        base.roles,
        MappingProxyType(
            {"knowledge-quality": replace(definition, routing=MappingProxyType(routes))}
        ),
    )
    crew = knowledge_package.KnowledgeQualityCrew(custom)
    request = _audit_request(_database(tmp_path))
    monkeypatch.setattr(
        knowledge_package,
        "_collect_observations",
        lambda audit: ([{"case_id": "case-1"}], {"read_only": True}),
    )

    async def success(ctx, resolved, **kwargs):
        return _invocation(resolved, _knowledge_payload(resolved.role.id))

    monkeypatch.setattr(knowledge_package, "execute_role", success)
    unknown = await crew.run(
        SimpleNamespace(), request=request, route="two_roles", workflow_id="wf"
    )
    assert unknown.blocked_reason == "crew.role.unknown:missing"

    def fail_resolve(*args, **kwargs):
        raise RoleResolutionError("schema_missing")

    monkeypatch.setattr(knowledge_package, "resolve_role", fail_resolve)
    resolved = await crew.run(
        SimpleNamespace(), request=request, route="retrieval_audit", workflow_id="wf"
    )
    assert resolved.blocked_reason.endswith(":schema_missing")
    monkeypatch.undo()
    monkeypatch.setattr(
        knowledge_package,
        "_collect_observations",
        lambda audit: ([{"case_id": "case-1"}], {"read_only": True}),
    )

    async def blocked_execute(ctx, resolved, **kwargs):
        return _invocation(resolved, blocked=True)

    monkeypatch.setattr(knowledge_package, "execute_role", blocked_execute)
    blocked = await crew.run(
        SimpleNamespace(), request=request, route="retrieval_audit", workflow_id="wf"
    )
    assert "crew.role.blocked:retrieval-researcher:stub_blocked" == blocked.blocked_reason

    async def invalid_execute(ctx, resolved, **kwargs):
        return _invocation(
            resolved, {"unbounded": "x" * (knowledge_package._MAX_PAYLOAD_TEXT_CHARS + 1)}
        )

    monkeypatch.setattr(knowledge_package, "execute_role", invalid_execute)
    invalid = await crew.run(
        SimpleNamespace(), request=request, route="retrieval_audit", workflow_id="wf"
    )
    assert invalid.blocked_reason == "crew.handoff.invalid:retrieval-researcher"

    routes = dict(definition.routing)
    routes["bad_second"] = CrewRoute(
        "bad_second", ("retrieval-researcher", "retrieval-validator"), False
    )
    custom = CrewCatalog(
        base.schema,
        base.roles,
        MappingProxyType(
            {"knowledge-quality": replace(definition, routing=MappingProxyType(routes))}
        ),
    )
    crew = knowledge_package.KnowledgeQualityCrew(custom)
    calls = 0

    def invalid_second(payload, schema):
        nonlocal calls
        calls += 1
        return [] if calls < 3 else ["invalid"]

    monkeypatch.setattr(knowledge_package, "validate_against_schema", invalid_second)
    monkeypatch.setattr(knowledge_package, "execute_role", success)
    second = await crew.run(
        SimpleNamespace(), request=request, route="bad_second", workflow_id="wf"
    )
    assert second.blocked_reason == "crew.role.input_invalid:retrieval-validator"


def test_software_schema_for_returns_independent_copy_and_rejects_unknown() -> None:
    from kiro_crew.crews.software_delivery.schemas import (
        SOFTWARE_DELIVERY_SCHEMAS,
        schema_for,
    )

    copied = schema_for("implementation_result")
    copied["required"].clear()
    assert SOFTWARE_DELIVERY_SCHEMAS["implementation_result"]["required"]
    with pytest.raises(KeyError):
        schema_for("missing")
