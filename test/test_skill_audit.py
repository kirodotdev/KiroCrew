"""Queue-wide skill overlap audit and re-stage-as-update tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.handlers import prompts as handlers
from kiro_crew.skills import AutoSkillProvenance, SkillsLoader


@pytest.fixture()
def loader(tmp_path):
    return SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)


class _Request:
    def __init__(self, loader, *, slug: str = "", body: object = None):
        self.app = {"state": SimpleNamespace(context_builder=SimpleNamespace(skills=loader))}
        self.match_info = {"slug": slug}
        self._body = {} if body is None else body

    async def json(self):
        return self._body


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


def test_audit_classifies_candidate_subsumed_by_live_triggers(loader):
    _live(loader, "review-live", "review code safely", "review code, review pull request")
    _pending(loader, "review-candidate", "inspect a change", "review code")

    relations = _relations(loader)
    assert len(relations) == 1
    assert relations[0]["classification"] == "subsumed"
    assert relations[0]["trigger_score"] == 0.5


def test_audit_classifies_moderate_pending_overlap(loader):
    _pending(loader, "deploy-one", "deploy static service", "publish demo")
    _pending(loader, "deploy-two", "deploy static website", "ship preview")

    relations = _relations(loader)
    assert len(relations) == 1
    assert relations[0]["classification"] == "overlapping"
    assert relations[0]["description_score"] == 0.5


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


def test_audit_rejects_inverted_thresholds(loader):
    with pytest.raises(ValueError, match="0 <= overlap <= duplicate <= 1"):
        loader.audit(duplicate_threshold=0.4, overlap_threshold=0.5)


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


@pytest.mark.asyncio
async def test_restage_endpoint_replaces_the_pending_candidate(loader):
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
        "target": "auto/deploy-live",
    }
    pending = loader.list_pending_skills()
    assert [item["slug"] for item in pending] == ["deploy-helper-update"]
    assert pending[0]["kind"] == "update"


@pytest.mark.asyncio
async def test_restage_endpoint_requires_a_target(loader):
    _pending(loader, "deploy-helper", "deploy helper", "deploy")

    response = await handlers.api_skill_pending_restage(
        _Request(loader, slug="deploy-helper", body={})
    )

    assert response.status == 400
    assert _payload(response)["code"] == "target_required"
