"""Queue-wide skill overlap audit and re-stage-as-update tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers import prompts as handlers
from kiro_crew.skills import AutoSkillProvenance, SkillsLoader


@pytest.fixture()
def loader(tmp_path, monkeypatch):
    config = KiroCrewConfig()
    monkeypatch.setattr("kiro_crew.skills.KiroCrewConfig.load", lambda: config)
    return SkillsLoader(
        skills_path=tmp_path / "skills",
        install_builtins=False,
        config=config,
    )


class _Request:
    def __init__(self, loader, *, slug: str = "", body: object = None):
        self.app = {"state": SimpleNamespace(context_builder=SimpleNamespace(skills=loader))}
        self.match_info = {"slug": slug}
        self._body = {} if body is None else body

    async def json(self):
        return self._body

    def get(self, _key, default=None):
        return default


def _payload(response) -> dict:
    return json.loads(response.body.decode())


def _provenance() -> AutoSkillProvenance:
    return AutoSkillProvenance(
        session_key="test",
        created_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    )


def _live(loader: SkillsLoader, name: str, description: str, triggers: str) -> None:
    skill_dir = loader._dir / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"triggers: {triggers}\n"
        "---\n\n"
        f"# {name}\n",
        encoding="utf-8",
    )
    loader._invalidate_iter_cache()


def _pending(loader: SkillsLoader, slug: str, description: str, triggers: str) -> None:
    assert loader.stage_skill_candidate(
        slug,
        description=description,
        triggers=triggers,
        procedure_md="## Steps\n\nRun it.",
        provenance=_provenance(),
    )


def _relations(loader: SkillsLoader) -> list[dict]:
    return [relation for cluster in loader.audit() for relation in cluster["relations"]]


def test_audit_classifies_an_exact_duplicate(loader):
    _live(loader, "deploy-live", "deploy a static website", "deploy website")
    _pending(loader, "deploy-copy", "deploy a static website", "deploy website")

    relations = _relations(loader)
    assert len(relations) == 1
    assert relations[0]["classification"] == "duplicate"
    assert relations[0]["score"] == 1.0


def test_audit_uses_configured_duplicate_threshold(loader, monkeypatch):
    config = KiroCrewConfig()
    config.skills.auto_similarity_threshold = 0.5
    monkeypatch.setattr("kiro_crew.skills.KiroCrewConfig.load", lambda: config)
    _live(loader, "deploy-live", "deploy static service", "publish demo")
    _pending(loader, "deploy-candidate", "deploy static website", "ship preview")

    relations = _relations(loader)
    assert len(relations) == 1
    assert relations[0]["classification"] == "duplicate"
    assert relations[0]["score"] == 0.5


def test_audit_classifies_candidate_subsumed_by_live_triggers(loader):
    _live(loader, "review-live", "review code safely", "review code, review pull request")
    _pending(loader, "review-candidate", "inspect a change", "review code")

    relations = _relations(loader)
    assert len(relations) == 1
    assert relations[0]["classification"] == "subsumed"
    assert set(relations[0]) == {"classification", "score", "members"}


def test_audit_classifies_candidate_covered_by_live_description(loader):
    # Candidate words {rotate, expired, tls, certificate} are all inside the live
    # description; Jaccard alone is only 4/7 (< 0.85), so coverage is what
    # classifies this pair as subsumed.
    _live(
        loader,
        "certs-live",
        "rotate expired tls certificate on the edge proxy fleet",
        "renew cert",
    )
    _pending(loader, "certs-candidate", "rotate expired tls certificate", "fix ssl")

    relations = _relations(loader)
    assert len(relations) == 1
    assert relations[0]["classification"] == "subsumed"


def test_audit_moderate_pending_live_similarity_is_overlapping_not_subsumed(loader):
    # Symmetric 0.5 Jaccard with no trigger subset and no containment: the
    # candidate is related to the live skill, not covered by it.
    _live(loader, "deploy-live", "deploy static service", "publish demo")
    _pending(loader, "deploy-candidate", "deploy static website", "ship preview")

    clusters = loader.audit()
    assert len(clusters) == 1
    assert clusters[0]["classification"] == "overlapping"
    assert clusters[0]["update_targets"] == []


def test_audit_never_pairs_two_builtin_skills(loader):
    from kiro_crew.skills import _PROVENANCE_MARKER

    _live(loader, "deploy-live", "deploy static service", "publish demo")
    _live(loader, "publish-live", "deploy static website", "ship preview")
    for name in ("deploy-live", "publish-live"):
        (loader._dir / name / _PROVENANCE_MARKER).write_text("{}", encoding="utf-8")

    assert loader.audit() == []

    # A pending candidate is still compared against builtins, and every relation
    # in the resulting cluster goes through the candidate -- never live-live.
    _pending(loader, "deploy-candidate", "deploy static website", "ship preview")
    clusters = loader.audit()
    assert len(clusters) == 1
    assert "pending:deploy-candidate" in {member["id"] for member in clusters[0]["members"]}
    assert all(
        "pending:deploy-candidate" in relation["members"] for relation in clusters[0]["relations"]
    )


def test_audit_classifies_moderate_pending_overlap(loader):
    _pending(loader, "deploy-one", "deploy static service", "publish demo")
    _pending(loader, "deploy-two", "deploy static website", "ship preview")

    relations = _relations(loader)
    assert len(relations) == 1
    assert relations[0]["classification"] == "overlapping"
    assert relations[0]["score"] == 0.5


def test_audit_omits_unrelated_skills(loader):
    _live(loader, "logs-live", "search application logs", "search logs")
    _pending(loader, "calendar-candidate", "schedule a team meeting", "book calendar")

    assert loader.audit() == []


def test_audit_compares_live_skills_with_each_other(loader):
    _live(loader, "deploy-live", "deploy static service", "publish demo")
    _live(loader, "publish-live", "deploy static website", "ship preview")

    clusters = loader.audit()
    assert len(clusters) == 1
    assert clusters[0]["classification"] == "overlapping"
    assert {member["id"] for member in clusters[0]["members"]} == {
        "live:deploy-live",
        "live:publish-live",
    }


def test_restage_as_update_reuses_pending_content_and_scripts(loader):
    assert loader.create_auto_skill(
        "deploy-live",
        description="deploy static sites",
        triggers="deploy site",
        procedure_md="## Steps\n\nOld steps.",
        provenance=_provenance(),
    )
    assert loader.stage_skill_candidate(
        "deploy-candidate",
        description="deploy static websites",
        triggers="deploy site",
        procedure_md="## Steps\n\nNew steps.",
        provenance=_provenance(),
        scripts=[{"filename": "check.py", "content": "print('ok')\n"}],
    )

    staged = loader.restage_as_update("deploy-candidate", "auto/deploy-live")

    assert staged == "auto/deploy-candidate-update"
    assert [item["slug"] for item in loader.list_pending_skills()] == ["deploy-candidate-update"]
    detail = loader.get_pending_skill("deploy-candidate-update")
    assert detail is not None
    assert detail["kind"] == "update"
    assert detail["target"] == "auto/deploy-live"
    assert detail["base_version"] == 1
    assert "New steps." in detail["content"]
    assert detail["scripts"] == [{"filename": "check.py", "content": "print('ok')\n"}]


def test_restage_as_update_requires_a_live_auto_target(loader):
    _live(loader, "hand-authored", "deploy static sites", "deploy site")
    _pending(loader, "deploy-candidate", "deploy static websites", "deploy site")

    assert loader.restage_as_update("deploy-candidate", "hand-authored") is None
    assert [item["slug"] for item in loader.list_pending_skills()] == ["deploy-candidate"]


@pytest.mark.asyncio
async def test_audit_endpoint_returns_clusters(loader):
    assert loader.create_auto_skill(
        "deploy-live",
        description="deploy helper",
        triggers="deploy",
        procedure_md="## Steps\n\nRun it.",
        provenance=_provenance(),
    )
    _pending(loader, "deploy-helper", "deploy helper", "deploy")

    response = await handlers.api_skills_audit(_Request(loader))

    assert response.status == 200
    cluster = _payload(response)["clusters"][0]
    assert cluster["classification"] == "duplicate"
    assert {member["id"] for member in cluster["members"]} == {
        "pending:deploy-helper",
        "live:auto/deploy-live",
    }
    assert cluster["update_targets"] == [
        {"pending_slug": "deploy-helper", "target": "auto/deploy-live"}
    ]


@pytest.mark.asyncio
async def test_restage_endpoint_replaces_the_pending_candidate(loader, monkeypatch):
    monkeypatch.setattr(handlers, "is_owner_dashboard_request", lambda _request: True)
    assert loader.create_auto_skill(
        "deploy-live",
        description="deploy helper",
        triggers="deploy",
        procedure_md="## Steps\n\nRun it.",
        provenance=_provenance(),
    )
    _pending(loader, "deploy-helper", "deploy helper", "deploy")

    response = await handlers.api_skill_pending_restage(
        _Request(
            loader,
            slug="deploy-helper",
            body={"target": "auto/deploy-live"},
        )
    )

    assert response.status == 200
    assert _payload(response) == {
        "staged": "auto/deploy-helper-update",
        "slug": "deploy-helper-update",
        "target": "auto/deploy-live",
    }
    pending = loader.list_pending_skills()
    assert [item["slug"] for item in pending] == ["deploy-helper-update"]
    assert pending[0]["kind"] == "update"


def test_restage_merges_live_metadata_and_returns_collision_slug(loader):
    assert loader.create_auto_skill(
        "deploy-live",
        description="deploy static sites",
        triggers="deploy site, publish preview",
        procedure_md="## Steps\n\nOld steps.",
        provenance=_provenance(),
    )
    _pending(loader, "deploy-candidate", "candidate-only description", "rollback deploy")
    _pending(loader, "deploy-candidate-update", "occupied", "occupied")

    staged = loader.restage_as_update("deploy-candidate", "auto/deploy-live")

    assert staged == "auto/deploy-candidate-update-2"
    detail = loader.get_pending_skill("deploy-candidate-update-2")
    assert detail is not None
    assert detail["meta"]["description"] == "deploy static sites"
    assert detail["meta"]["triggers"] == ("deploy site, publish preview, rollback deploy")


@pytest.mark.asyncio
async def test_restage_endpoint_undo_restores_original_candidate(loader, monkeypatch):
    monkeypatch.setattr(handlers, "is_owner_dashboard_request", lambda _request: True)
    assert loader.create_auto_skill(
        "deploy-live",
        description="deploy helper",
        triggers="deploy",
        procedure_md="## Steps\n\nRun it.",
        provenance=_provenance(),
    )
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    restaged = await handlers.api_skill_pending_restage(
        _Request(loader, slug="deploy-helper", body={"target": "auto/deploy-live"})
    )

    response = await handlers.api_skill_pending_restage_undo(
        _Request(loader, slug=_payload(restaged)["slug"])
    )

    assert response.status == 200
    assert _payload(response) == {
        "restored": "auto/deploy-helper",
        "slug": "deploy-helper",
    }
    assert [item["slug"] for item in loader.list_pending_skills()] == ["deploy-helper"]
    assert not loader._restage_backup_path("deploy-helper-update").exists()


@pytest.mark.asyncio
async def test_restage_endpoint_returns_collision_resolved_slug(loader, monkeypatch):
    monkeypatch.setattr(handlers, "is_owner_dashboard_request", lambda _request: True)
    assert loader.create_auto_skill(
        "deploy-live",
        description="deploy helper",
        triggers="deploy",
        procedure_md="## Steps\n\nRun it.",
        provenance=_provenance(),
    )
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    _pending(loader, "deploy-helper-update", "occupied", "occupied")

    response = await handlers.api_skill_pending_restage(
        _Request(loader, slug="deploy-helper", body={"target": "auto/deploy-live"})
    )

    assert response.status == 200
    assert _payload(response)["staged"] == "auto/deploy-helper-update-2"
    assert _payload(response)["slug"] == "deploy-helper-update-2"


@pytest.mark.asyncio
async def test_restage_name_exhaustion_preserves_original_candidate(loader, monkeypatch):
    monkeypatch.setattr(handlers, "is_owner_dashboard_request", lambda _request: True)
    assert loader.create_auto_skill(
        "deploy-live",
        description="deploy helper",
        triggers="deploy",
        procedure_md="## Steps\n\nRun it.",
        provenance=_provenance(),
    )
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    for index in range(1, 51):
        suffix = "" if index == 1 else f"-{index}"
        _pending(
            loader,
            f"deploy-helper-update{suffix}",
            "occupied pending proposal",
            "occupied",
        )

    response = await handlers.api_skill_pending_restage(
        _Request(
            loader,
            slug="deploy-helper",
            body={"target": "auto/deploy-live"},
        )
    )

    assert response.status == 409
    assert _payload(response) == {
        "error": "candidate or live auto-skill target was not found",
        "code": "restage_rejected",
    }
    assert loader.get_pending_skill("deploy-helper") is not None
    assert len(loader.list_pending_skills()) == 51


@pytest.mark.asyncio
async def test_restage_endpoint_requires_a_target(loader, monkeypatch):
    monkeypatch.setattr(handlers, "is_owner_dashboard_request", lambda _request: True)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")

    response = await handlers.api_skill_pending_restage(
        _Request(loader, slug="deploy-helper", body={})
    )

    assert response.status == 400
    assert _payload(response)["code"] == "target_required"


def test_restage_captures_base_version_before_reading_live_body(loader, monkeypatch):
    assert loader.create_auto_skill(
        "deploy-live",
        description="deploy helper",
        triggers="deploy",
        procedure_md="## Steps\n\nVersion one.",
        provenance=_provenance(),
    )
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    version = {"value": 1}
    read_live = loader.read_auto_skill_body

    def read_then_advance(name: str):
        body = read_live(name)
        version["value"] = 2
        return body

    monkeypatch.setattr(loader, "get_auto_skill_version", lambda _name: version["value"])
    monkeypatch.setattr(loader, "read_auto_skill_body", read_then_advance)

    staged = loader.restage_as_update("deploy-helper", "auto/deploy-live")

    assert staged == "auto/deploy-helper-update"
    detail = loader.get_pending_skill("deploy-helper-update")
    assert detail is not None
    assert detail["base_version"] == 1


def test_skill_audit_payload_is_bounded_and_references_retained_members():
    members = [
        {
            "id": f"pending:p-{index}",
            "kind": "pending",
            "name": f"auto/p-{index}",
            "slug": f"p-{index}",
        }
        for index in range(10)
    ] + [
        {"id": f"live:auto/l-{index}", "kind": "live", "name": f"auto/l-{index}"}
        for index in range(15)
    ]
    relations = [
        {
            "classification": "duplicate",
            "score": 1.0,
            "members": [f"pending:p-{index % 10}", f"live:auto/l-{index % 10}"],
        }
        for index in range(45)
    ]
    targets = [
        {"pending_slug": f"p-{index % 10}", "target": f"auto/l-{index % 10}"} for index in range(25)
    ]
    cluster = {
        "classification": "duplicate",
        "score": 1.0,
        "members": members,
        "relations": relations,
        "update_targets": targets,
    }

    bounded = handlers._bound_skill_audit_clusters([cluster] * 55)

    assert len(bounded) == handlers._SKILL_AUDIT_MAX_CLUSTERS
    first = bounded[0]
    assert len(first["members"]) == handlers._SKILL_AUDIT_MAX_MEMBERS
    assert len(first["relations"]) == handlers._SKILL_AUDIT_MAX_RELATIONS
    assert len(first["update_targets"]) == handlers._SKILL_AUDIT_MAX_UPDATE_TARGETS
    assert first["omitted_members"] == 5
    returned_ids = {member["id"] for member in first["members"]}
    assert all(set(relation["members"]) <= returned_ids for relation in first["relations"])


@pytest.mark.asyncio
async def test_restage_undo_sel_audits_denied_bad_request_rejected_and_error(loader, monkeypatch):
    events: list[dict] = []
    sel = SimpleNamespace(
        log_api_access=lambda **_kwargs: None,
        log_tool_invocation=lambda **kwargs: events.append(kwargs),
    )
    monkeypatch.setattr(handlers, "_sel", lambda: sel)

    monkeypatch.setattr(handlers, "is_owner_dashboard_request", lambda _request: False)
    denied = await handlers.api_skill_pending_restage_undo(
        _Request(loader, slug="deploy-helper-update")
    )
    assert denied.status == 403

    monkeypatch.setattr(handlers, "is_owner_dashboard_request", lambda _request: True)
    invalid = await handlers.api_skill_pending_restage_undo(_Request(loader, slug="../bad"))
    assert invalid.status == 400

    missing = await handlers.api_skill_pending_restage_undo(
        _Request(loader, slug="deploy-helper-update")
    )
    assert missing.status == 409

    assert loader.create_auto_skill(
        "deploy-live",
        description="deploy helper",
        triggers="deploy",
        procedure_md="## Steps\n\nRun it.",
        provenance=_provenance(),
    )
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    assert loader.restage_as_update("deploy-helper", "auto/deploy-live")
    restored = await handlers.api_skill_pending_restage_undo(
        _Request(loader, slug="deploy-helper-update")
    )
    assert restored.status == 200

    def fail_undo(_slug: str):
        raise OSError("disk failure")

    monkeypatch.setattr(loader, "undo_restage_as_update", fail_undo)
    failed = await handlers.api_skill_pending_restage_undo(
        _Request(loader, slug="deploy-helper-update")
    )
    assert failed.status == 500

    assert [event["outcome"] for event in events] == [
        "denied",
        "bad_request",
        "rejected",
        "ok",
        "error",
    ]
