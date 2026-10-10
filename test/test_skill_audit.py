"""Queue-wide skill overlap audit and re-stage-as-update tests."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers import prompts as handlers
from kiro_crew.skills import AutoSkillProvenance, SkillsLoader


@pytest.fixture(autouse=True)
def _close_loaders(close_skills_loaders):
    """Close every SkillsLoader this module builds, the fixture's included."""


@pytest.fixture()
def loader(tmp_path, monkeypatch, _close_loaders):
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
    return [relation for cluster in loader.audit()["clusters"] for relation in cluster["relations"]]


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

    clusters = loader.audit()["clusters"]
    assert len(clusters) == 1
    assert clusters[0]["classification"] == "overlapping"
    assert clusters[0]["update_targets"] == []


def test_audit_never_pairs_two_builtin_skills(loader):
    from kiro_crew.skills import _PROVENANCE_MARKER

    _live(loader, "deploy-live", "deploy static service", "publish demo")
    _live(loader, "publish-live", "deploy static website", "ship preview")
    for name in ("deploy-live", "publish-live"):
        (loader._dir / name / _PROVENANCE_MARKER).write_text("{}", encoding="utf-8")

    assert loader.audit()["clusters"] == []

    # A pending candidate is still compared against builtins, and every relation
    # in the resulting cluster goes through the candidate -- never live-live.
    _pending(loader, "deploy-candidate", "deploy static website", "ship preview")
    clusters = loader.audit()["clusters"]
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

    assert loader.audit()["clusters"] == []


def test_audit_compares_live_skills_with_each_other(loader):
    _live(loader, "deploy-live", "deploy static service", "publish demo")
    _live(loader, "publish-live", "deploy static website", "ship preview")

    clusters = loader.audit()["clusters"]
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
    assert [item["slug"] for item in loader.list_pending_skills()] == [
        "deploy-candidate",
        "deploy-candidate-update",
    ]
    detail = loader.get_pending_skill("deploy-candidate-update")
    assert detail is not None
    assert detail["kind"] == "update"
    assert detail["target"] == "auto/deploy-live"
    assert detail["base_version"] == 1
    assert "New steps." in detail["content"]
    assert detail["scripts"] == [{"filename": "check.py", "content": "print('ok')\n"}]


def test_restage_refuses_a_linked_digest_record_without_touching_its_target(loader, tmp_path):
    _deploy_live(loader)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    versions = loader._versions_root("deploy-live")
    versions.mkdir(parents=True)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("unchanged", encoding="ascii")
    record = versions / "restage-deploy-helper-update.sha256"
    record.symlink_to(sentinel)

    assert loader.restage_as_update("deploy-helper", "auto/deploy-live") is None

    assert sentinel.read_text(encoding="ascii") == "unchanged"
    assert not record.exists()
    assert [item["slug"] for item in loader.list_pending_skills()] == ["deploy-helper"]


def test_restage_refuses_a_linked_versions_directory_without_touching_its_target(loader, tmp_path):
    _deploy_live(loader)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    external = tmp_path / "external-versions"
    external.mkdir()
    sentinel = external / "restage-deploy-helper-update.sha256"
    sentinel.write_text("unchanged", encoding="ascii")
    versions = loader._versions_root("deploy-live")
    versions.symlink_to(external, target_is_directory=True)

    assert loader.restage_as_update("deploy-helper", "auto/deploy-live") is None

    assert sentinel.read_text(encoding="ascii") == "unchanged"
    assert versions.is_symlink()
    assert [item["slug"] for item in loader.list_pending_skills()] == ["deploy-helper"]


def test_approval_refuses_a_linked_restage_digest_record(loader, tmp_path, monkeypatch):
    from kiro_crew.skills import PendingApprovalRefused

    _deploy_live(loader)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    assert loader.restage_as_update("deploy-helper", "auto/deploy-live")
    record = loader._versions_root("deploy-live") / "restage-deploy-helper-update.sha256"
    expected = record.read_text(encoding="ascii")
    record.unlink()
    external = tmp_path / "external-digest"
    external.write_text(expected, encoding="ascii")
    record.symlink_to(external)
    # Isolate the digest reader's defense in depth from the broader live-tree
    # guard, which independently rejects any symlink under the live skill.
    monkeypatch.setattr(loader, "_candidate_has_symlink", lambda _path: False)

    with pytest.raises(PendingApprovalRefused) as refused:
        loader.approve_pending_update_checked("deploy-helper-update")

    assert refused.value.reason == "stale_base"
    assert external.read_text(encoding="ascii") == expected
    assert (loader._pending_root() / "deploy-helper-update" / "SKILL.md").is_file()


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
    payload = _payload(response)
    assert payload["omitted_entries"] == 0
    assert payload["omitted_relations"] == 0
    cluster = payload["clusters"][0]
    assert cluster["classification"] == "duplicate"
    assert {member["id"] for member in cluster["members"]} == {
        "pending:deploy-helper",
        "live:auto/deploy-live",
    }
    assert cluster["update_targets"] == [
        {"pending_slug": "deploy-helper", "target": "auto/deploy-live"}
    ]


@pytest.mark.asyncio
async def test_restage_endpoint_adds_update_and_keeps_the_original(loader, monkeypatch):
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
    assert [item["slug"] for item in pending] == ["deploy-helper", "deploy-helper-update"]
    assert pending[1]["kind"] == "update"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slug", "body", "code"),
    [
        ("../bad", {"target": "auto/deploy-live"}, "invalid_slug"),
        ("deploy-helper", {}, "target_required"),
        ("deploy-helper", {"target": "auto/" + "x" * 300}, "target_too_long"),
        ("d" * 8000, {"target": "auto/deploy-live"}, "slug_too_long"),
    ],
)
async def test_restage_bad_requests_are_audited(loader, monkeypatch, slug, body, code):
    monkeypatch.setattr(handlers, "is_owner_dashboard_request", lambda _request: True)
    events: list[dict] = []
    monkeypatch.setattr(
        handlers,
        "_sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kwargs: events.append(kwargs)),
    )

    response = await handlers.api_skill_pending_restage(_Request(loader, slug=slug, body=body))

    assert response.status == 400
    assert _payload(response)["code"] == code
    restage = [e for e in events if e["tool_name"] == "api_skill_pending_restage"]
    assert restage and restage[-1]["outcome"] == "bad_request"
    assert restage[-1]["metadata"]["code"] == code
    assert len(restage[-1]["metadata"]["slug"]) <= handlers._SKILL_RESTAGE_MAX_TARGET_CHARS


@pytest.mark.asyncio
async def test_restage_field_limit_refusal_is_audited(loader, monkeypatch):
    from kiro_crew import skills as skills_mod

    monkeypatch.setattr(handlers, "is_owner_dashboard_request", lambda _request: True)
    events: list[dict] = []
    monkeypatch.setattr(
        handlers,
        "_sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kwargs: events.append(kwargs)),
    )
    _deploy_live(loader)
    _pending(loader, "deploy-helper", "x" * (skills_mod._AUDIT_MAX_TEXT_CHARS + 1), "deploy")

    response = await handlers.api_skill_pending_restage(
        _Request(loader, slug="deploy-helper", body={"target": "auto/deploy-live"})
    )

    assert response.status == 409
    assert _payload(response)["code"] == "restage_field_too_long"
    restage = [e for e in events if e["tool_name"] == "api_skill_pending_restage"]
    assert restage[-1]["outcome"] == "rejected"
    assert restage[-1]["metadata"]["code"] == "restage_field_too_long"


def test_payload_counts_update_targets_it_drops():
    members = [
        {
            "id": f"pending:p-{index}",
            "kind": "pending",
            "name": f"auto/p-{index}",
            "slug": f"p-{index}",
        }
        for index in range(10)
    ] + [{"id": "live:auto/l", "kind": "live", "name": "auto/l"}]
    relations = [
        {
            "classification": "overlapping",
            "score": 0.5,
            "members": [f"pending:p-{index}", "live:auto/l"],
        }
        for index in range(10)
    ]
    cluster = {
        "classification": "overlapping",
        "score": 0.5,
        "members": members,
        "relations": relations,
        "update_targets": [
            {"pending_slug": f"p-{index}", "target": "auto/l"} for index in range(10)
        ],
    }
    bounded = handlers._bound_skill_audit_clusters([cluster])[0]
    assert len(bounded["update_targets"]) + bounded["omitted_update_targets"] == 10
    assert bounded["omitted_update_targets"] > 0


def test_restage_replaces_oversized_provenance_values(loader):
    assert loader.create_auto_skill(
        "deploy-live",
        description="deploy helper",
        triggers="deploy",
        procedure_md="## Steps\n\nRun it.",
        provenance=_provenance(),
    )
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    skill_md = loader._pending_root() / "deploy-helper" / "SKILL.md"
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8").replace(
            "session_key: ", "session_key: " + "k" * 1500, 1
        ),
        encoding="utf-8",
    )
    meta_path = loader._pending_root() / "deploy-helper" / ".meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["source"] = "s" * 1500
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    staged = loader.restage_as_update("deploy-helper", "auto/deploy-live")

    assert staged == "auto/deploy-helper-update"
    new_dir = loader._pending_root() / "deploy-helper-update"
    new_meta = json.loads((new_dir / ".meta.json").read_text(encoding="utf-8"))
    assert new_meta["source"] == "consolidation"
    assert "k" * 300 not in (new_dir / "SKILL.md").read_text(encoding="utf-8")


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


def test_restage_reads_base_version_from_the_bounded_live_body(loader, monkeypatch):
    assert loader.create_auto_skill(
        "deploy-live",
        description="deploy helper",
        triggers="deploy",
        procedure_md="## Steps\n\nVersion one.",
        provenance=_provenance(),
    )
    live_md = loader._dir / "auto" / "deploy-live" / "SKILL.md"
    _set_live_version(live_md, 3)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")

    def unbounded(*_args, **_kwargs):
        raise AssertionError("restage must not read the live skill unbounded")

    monkeypatch.setattr(loader, "get_auto_skill_version", unbounded)
    monkeypatch.setattr(loader, "_cached_frontmatter", unbounded)

    staged = loader.restage_as_update("deploy-helper", "auto/deploy-live")

    assert staged == "auto/deploy-helper-update"
    detail = loader.get_pending_skill("deploy-helper-update")
    assert detail is not None
    assert detail["base_version"] == 3


def _set_live_version(live_md, version: int) -> None:
    text = live_md.read_text(encoding="utf-8")
    lines = [line for line in text.split("\n") if not line.startswith("version:")]
    lines.insert(1, f"version: {version}")
    live_md.write_text("\n".join(lines), encoding="utf-8")


def test_restage_base_version_is_the_version_of_the_body_it_read(loader, monkeypatch):
    assert loader.create_auto_skill(
        "deploy-live",
        description="deploy helper",
        triggers="deploy",
        procedure_md="## Steps\n\nVersion one.",
        provenance=_provenance(),
    )
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    live_md = loader._dir / "auto" / "deploy-live" / "SKILL.md"
    read_live = loader.read_auto_skill_body

    def read_then_advance(name: str, **kwargs):
        body = read_live(name, **kwargs)
        _set_live_version(live_md, 2)
        return body

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
    assert first["omitted_relations"] == 37
    returned_ids = {member["id"] for member in first["members"]}
    assert all(set(relation["members"]) <= returned_ids for relation in first["relations"])


def test_audit_never_pairs_a_builtin_with_another_live_skill(loader):
    from kiro_crew.skills import _PROVENANCE_MARKER

    _live(loader, "deploy-live", "deploy static service", "publish demo")
    _live(loader, "publish-live", "deploy static website", "ship preview")
    (loader._dir / "deploy-live" / _PROVENANCE_MARKER).write_text("{}", encoding="utf-8")

    assert loader.audit()["clusters"] == []


def test_audit_bounds_admitted_entries_and_retained_relations(loader, monkeypatch):
    monkeypatch.setattr("kiro_crew.skills._AUDIT_MAX_ENTRIES", 3)
    monkeypatch.setattr("kiro_crew.skills._AUDIT_MAX_RELATIONS", 2)
    for index in range(6):
        _pending(loader, f"deploy-{index}", "deploy a static website", "deploy website")

    audit = loader.audit()
    clusters = audit["clusters"]

    relations = [relation for cluster in clusters for relation in cluster["relations"]]
    members = {member["id"] for cluster in clusters for member in cluster["members"]}
    assert len(relations) == 2
    assert len(members) <= 3
    assert audit["omitted_entries"] == 3
    assert audit["omitted_relations"] == 1


def test_audit_caps_untrusted_text_before_building_word_sets(loader, monkeypatch):
    monkeypatch.setattr("kiro_crew.skills._AUDIT_MAX_TEXT_CHARS", 24)
    seen: list[str] = []
    original = loader._description_words

    def capture(value: str):
        seen.append(value)
        return original(value)

    monkeypatch.setattr(loader, "_description_words", capture)
    _pending(loader, "long-description", "x" * 200, "trigger")
    _live(loader, "long-live", "x" * 200, "trigger")
    loader.audit()
    assert seen
    assert all(len(value) <= 24 for value in seen)


def test_payload_keeps_late_relation_backing_retained_update_target():
    members = [
        {
            "id": f"pending:p-{index}",
            "kind": "pending",
            "name": f"auto/p-{index}",
            "slug": f"p-{index}",
        }
        for index in range(9)
    ] + [
        {"id": f"live:auto/l-{index}", "kind": "live", "name": f"auto/l-{index}"}
        for index in range(9)
    ]
    relations = [
        {
            "classification": "overlapping",
            "score": 0.5,
            "members": [f"pending:p-{index}", f"live:auto/l-{index}"],
        }
        for index in range(8)
    ] + [{"classification": "subsumed", "score": 0.9, "members": ["pending:p-8", "live:auto/l-8"]}]
    cluster = {
        "classification": "subsumed",
        "score": 0.9,
        "members": members,
        "relations": relations,
        "update_targets": [{"pending_slug": "p-8", "target": "auto/l-8"}],
    }
    bounded = handlers._bound_skill_audit_clusters([cluster])[0]
    assert bounded["update_targets"] == [{"pending_slug": "p-8", "target": "auto/l-8"}]
    assert any(set(r["members"]) == {"pending:p-8", "live:auto/l-8"} for r in bounded["relations"])
    assert bounded["omitted_relations"] == 1


def test_payload_keeps_pending_member_listed_after_twenty_live_members():
    live = [
        {"id": f"live:auto/a-{index:02d}", "kind": "live", "name": f"auto/a-{index:02d}"}
        for index in range(25)
    ]
    pending = {"id": "pending:z-late", "kind": "pending", "name": "auto/z-late", "slug": "z-late"}
    cluster = {
        "classification": "overlapping",
        "score": 0.6,
        "members": [*live, pending],
        "relations": [
            {
                "classification": "overlapping",
                "score": 0.6,
                "members": ["pending:z-late", "live:auto/a-24"],
            }
        ],
        "update_targets": [{"pending_slug": "z-late", "target": "auto/a-24"}],
    }
    bounded = handlers._bound_skill_audit_clusters([cluster])[0]
    ids = [member["id"] for member in bounded["members"]]
    assert ids[:2] == ["pending:z-late", "live:auto/a-24"]
    assert bounded["update_targets"] == [{"pending_slug": "z-late", "target": "auto/a-24"}]
    assert bounded["omitted_members"] == 6


def test_audit_names_pending_members_from_slug_not_meta(loader):
    _live(loader, "deploy-live", "deploy a static website", "deploy website")
    _pending(loader, "deploy-copy", "deploy a static website", "deploy website")
    meta_path = loader._pending_root() / "deploy-copy" / ".meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["name"] = "x" * 30_000
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    names = {
        member["name"] for cluster in loader.audit()["clusters"] for member in cluster["members"]
    }
    assert "auto/deploy-copy" in names
    assert max(len(name) for name in names) < 300


def test_audit_omits_and_counts_an_overlong_live_name(loader, monkeypatch):
    from kiro_crew import skills as skills_module

    _live(loader, "deploy-live", "deploy a static website", "deploy website")
    _live(loader, "deploy-live-longer", "deploy a static website", "deploy website")
    monkeypatch.setattr(skills_module, "_AUDIT_MAX_NAME_CHARS", len("deploy-live"))
    result = loader.audit()
    names = {member["name"] for cluster in result["clusters"] for member in cluster["members"]}
    assert "deploy-live-longer" not in names
    assert result["omitted_entries"] == 1


def test_audit_omits_an_oversized_live_skill_without_caching_it(loader, monkeypatch):
    import kiro_crew.skills as skills_mod

    _deploy_live(loader)
    monkeypatch.setattr(skills_mod, "_AUDIT_MAX_META_BYTES", 2048)
    loader.create_skill(
        "huge-live", "---\nname: huge-live\ndescription: " + "deploy " * 1000 + "\n---\n"
    )
    loader._fm_cache.clear()

    result = loader.audit()

    names = {member["name"] for cluster in result["clusters"] for member in cluster["members"]}
    assert "huge-live" not in names
    assert result["omitted_entries"] == 1
    assert loader._fm_cache == {}


def test_restage_of_max_length_slug_keeps_collision_name_in_grammar(loader):
    from kiro_crew.skills import _AUTO_NAME_PATTERN

    assert loader.create_auto_skill(
        "deploy-live",
        description="deploy helper",
        triggers="deploy",
        procedure_md="## Steps\n\nRun it.",
        provenance=_provenance(),
    )
    long_slug = "d" * 63
    assert _AUTO_NAME_PATTERN.match(long_slug)
    _pending(loader, long_slug, "deploy helper", "deploy")
    first = loader.restage_as_update(long_slug, "auto/deploy-live")
    assert first is not None
    assert _AUTO_NAME_PATTERN.match(first.split("/", 1)[1])
    assert first.split("/", 1)[1] in {item["slug"] for item in loader.list_pending_skills()}


def _deploy_live(loader: SkillsLoader) -> None:
    assert loader.create_auto_skill(
        "deploy-live",
        description="deploy helper",
        triggers="deploy",
        procedure_md="## Steps\n\nRun it.",
        provenance=_provenance(),
    )


def test_repeat_restage_returns_the_pending_proposal(loader):
    _deploy_live(loader)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")

    first = loader.restage_as_update("deploy-helper", "auto/deploy-live")
    second = loader.restage_as_update("deploy-helper", "auto/deploy-live")

    assert first == second == "auto/deploy-helper-update"
    assert sorted(item["slug"] for item in loader.list_pending_skills()) == [
        "deploy-helper",
        "deploy-helper-update",
    ]


def test_restage_refuses_a_live_target_over_the_byte_cap(loader, monkeypatch):
    from kiro_crew import skills as skills_mod

    _deploy_live(loader)
    live_md = loader._dir / "auto" / "deploy-live" / "SKILL.md"
    live_md.write_text(live_md.read_text(encoding="utf-8") + "x" * 5000, encoding="utf-8")
    monkeypatch.setattr(skills_mod, "_AUDIT_MAX_META_BYTES", 2048)
    loader._invalidate_iter_cache()
    _pending(loader, "deploy-helper", "deploy helper", "deploy")

    assert loader.restage_as_update("deploy-helper", "auto/deploy-live") is None
    assert sorted(item["slug"] for item in loader.list_pending_skills()) == ["deploy-helper"]


@pytest.mark.parametrize(
    ("owner", "field"),
    [
        ("live", "description"),
        ("live", "triggers"),
        ("candidate", "description"),
        ("candidate", "triggers"),
    ],
)
def test_restage_refuses_metadata_fields_over_the_text_cap(loader, owner, field):
    from kiro_crew import skills as skills_mod
    from kiro_crew.skills import RestageRefused

    _deploy_live(loader)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    huge = "x" * (skills_mod._AUDIT_MAX_TEXT_CHARS + 1)
    if owner == "live":
        live_md = loader._dir / "auto" / "deploy-live" / "SKILL.md"
        text = live_md.read_text(encoding="utf-8")
        old = "description: deploy helper" if field == "description" else "triggers: deploy"
        text = text.replace(old, f"{field}: {huge}", 1)
        live_md.write_text(text, encoding="utf-8")
        loader._invalidate_iter_cache()
    else:
        meta_path = loader._pending_root() / "deploy-helper" / ".meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta[field] = huge
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(RestageRefused) as refused:
        loader.restage_as_update("deploy-helper", "auto/deploy-live")

    assert refused.value.reason == "restage_field_too_long"
    assert [item["slug"] for item in loader.list_pending_skills()] == ["deploy-helper"]


def test_crlf_live_body_digest_agrees_across_restage_preview_and_approval(loader):
    _deploy_live(loader)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    live_md = loader._dir / "auto" / "deploy-live" / "SKILL.md"
    live_md.write_bytes(live_md.read_bytes().replace(b"\n", b"\r\n"))
    loader._invalidate_iter_cache()

    assert loader.restage_as_update("deploy-helper", "auto/deploy-live")
    preview = loader.preview_pending_update("deploy-helper-update")

    assert preview is not None
    assert preview["stale_base"] is False
    assert loader.approve_pending_update_checked("deploy-helper-update") == "auto/deploy-live"


def test_approval_serializes_dashboard_edit_from_digest_check_through_write(loader, monkeypatch):
    _deploy_live(loader)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    assert loader.restage_as_update("deploy-helper", "auto/deploy-live")
    live_md = loader._dir / "auto" / "deploy-live" / "SKILL.md"
    edited = live_md.read_text(encoding="utf-8").replace("Run it.", "Dashboard edit survives.")
    approval_inside_lock = threading.Event()
    release_approval = threading.Event()
    writer_done = threading.Event()
    approval_result: list[str] = []
    original_rewrite = loader._rewrite_update_frontmatter

    def pause_after_validation(*args, **kwargs):
        approval_inside_lock.set()
        assert not writer_done.wait(0.1)
        assert release_approval.wait(2)
        return original_rewrite(*args, **kwargs)

    monkeypatch.setattr(loader, "_rewrite_update_frontmatter", pause_after_validation)

    def approve():
        approval_result.append(loader.approve_pending_update_checked("deploy-helper-update"))

    def edit():
        assert loader.update_skill("auto/deploy-live", edited)
        writer_done.set()

    approval_thread = threading.Thread(target=approve)
    approval_thread.start()
    assert approval_inside_lock.wait(2)
    writer_thread = threading.Thread(target=edit)
    writer_thread.start()
    assert not writer_done.wait(0.1)
    release_approval.set()
    approval_thread.join(2)
    writer_thread.join(2)

    assert approval_result == ["auto/deploy-live"]
    assert writer_done.is_set()
    assert live_md.read_text(encoding="utf-8") == edited


def test_restage_against_a_newer_target_version_stages_a_new_proposal(loader, monkeypatch):
    _deploy_live(loader)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    first = loader.restage_as_update("deploy-helper", "auto/deploy-live")
    _set_live_version(loader._dir / "auto" / "deploy-live" / "SKILL.md", 2)

    second = loader.restage_as_update("deploy-helper", "auto/deploy-live")

    assert first == "auto/deploy-helper-update"
    assert second == "auto/deploy-helper-update-2"


def _hand_edit_live(loader: SkillsLoader) -> str:
    """Edit the live body the way the dashboard does: same ``version``, new text."""
    live_md = loader._dir / "auto" / "deploy-live" / "SKILL.md"
    edited = live_md.read_text(encoding="utf-8").replace("Run it.", "Run it twice, by hand.")
    assert loader.update_skill("auto/deploy-live", edited)
    return edited


def test_restage_records_the_digest_of_the_live_body(loader):
    from kiro_crew import skills as skills_mod

    _deploy_live(loader)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    live = (loader._dir / "auto" / "deploy-live" / "SKILL.md").read_text(encoding="utf-8")

    assert loader.restage_as_update("deploy-helper", "auto/deploy-live")

    meta = loader._read_pending_meta("deploy-helper-update")
    assert meta["base_digest"] == skills_mod._live_body_digest(live)


def test_approval_refuses_an_update_over_a_hand_edited_live_body(loader):
    from kiro_crew.skills import PendingApprovalRefused

    _deploy_live(loader)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    assert loader.restage_as_update("deploy-helper", "auto/deploy-live")
    edited = _hand_edit_live(loader)

    diff = loader.preview_pending_update("deploy-helper-update")
    with pytest.raises(PendingApprovalRefused) as refused:
        loader.approve_pending_update_checked("deploy-helper-update")

    assert diff is not None and diff["stale_base"] is True
    assert refused.value.reason == "stale_base"
    live_md = loader._dir / "auto" / "deploy-live" / "SKILL.md"
    assert live_md.read_text(encoding="utf-8") == edited
    assert (loader._pending_root() / "deploy-helper-update" / "SKILL.md").is_file()


def test_repeat_restage_after_a_hand_edit_stages_a_new_proposal(loader):
    _deploy_live(loader)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    first = loader.restage_as_update("deploy-helper", "auto/deploy-live")
    _hand_edit_live(loader)

    second = loader.restage_as_update("deploy-helper", "auto/deploy-live")

    assert first == "auto/deploy-helper-update"
    assert second == "auto/deploy-helper-update-2"


@pytest.mark.parametrize("tamper", ["removed", "malformed", "rewritten"])
def test_approval_fails_closed_when_the_restage_digest_is_tampered(loader, tamper):
    from kiro_crew.skills import PendingApprovalRefused

    _deploy_live(loader)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    assert loader.restage_as_update("deploy-helper", "auto/deploy-live")
    edited = _hand_edit_live(loader)
    meta_path = loader._pending_root() / "deploy-helper-update" / ".meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if tamper == "removed":
        del meta["base_digest"]
    elif tamper == "malformed":
        meta["base_digest"] = 7
    else:
        meta["base_digest"] = "0" * 64
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    diff = loader.preview_pending_update("deploy-helper-update")
    with pytest.raises(PendingApprovalRefused) as refused:
        loader.approve_pending_update_checked("deploy-helper-update")

    assert diff is not None and diff["stale_base"] is True
    assert refused.value.reason == "stale_base"
    live_md = loader._dir / "auto" / "deploy-live" / "SKILL.md"
    assert live_md.read_text(encoding="utf-8") == edited


def test_approval_fails_closed_when_both_digest_copies_are_gone(loader):
    from kiro_crew import skills as skills_mod
    from kiro_crew.skills import PendingApprovalRefused

    _deploy_live(loader)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    assert loader.restage_as_update("deploy-helper", "auto/deploy-live")
    skills_mod._restage_base_path(loader, "deploy-live", "deploy-helper-update").unlink()
    meta_path = loader._pending_root() / "deploy-helper-update" / ".meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    del meta["base_digest"]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(PendingApprovalRefused) as refused:
        loader.approve_pending_update_checked("deploy-helper-update")

    assert refused.value.reason == "stale_base"


def test_restage_digest_record_is_removed_with_its_proposal(loader):
    from kiro_crew import skills as skills_mod

    _deploy_live(loader)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    _pending(loader, "deploy-other", "deploy helper", "deploy")
    assert loader.restage_as_update("deploy-helper", "auto/deploy-live")
    assert loader.restage_as_update("deploy-other", "auto/deploy-live")
    dismissed = skills_mod._restage_base_path(loader, "deploy-live", "deploy-helper-update")
    approved = skills_mod._restage_base_path(loader, "deploy-live", "deploy-other-update")
    assert dismissed.is_file() and approved.is_file()

    assert loader.dismiss_pending_skill("deploy-helper-update")
    assert loader.approve_pending_update_checked("deploy-other-update") == "auto/deploy-live"

    assert not dismissed.exists()
    assert not approved.exists()


def test_approval_refuses_a_target_rewritten_after_its_lock_was_taken(loader, monkeypatch):
    from kiro_crew.skills import PendingApprovalRefused

    _deploy_live(loader)
    assert loader.create_auto_skill(
        "deploy-other",
        description="other helper",
        triggers="other",
        procedure_md="## Steps\n\nOther.",
        provenance=_provenance(),
    )
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    # A crystallize-style update (no restage digest), so only the lock check
    # can catch the rewrite: both targets sit at the same version.
    meta_path = loader._pending_root() / "deploy-helper" / ".meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update(kind="update", target="auto/deploy-live", base_version=1)
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    other_md = loader._dir / "auto" / "deploy-other" / "SKILL.md"
    other_before = other_md.read_text(encoding="utf-8")
    original_read = loader._read_pending_meta
    reads = {"n": 0}

    def rewrite_after_first_read(slug):
        meta = original_read(slug)
        reads["n"] += 1
        if reads["n"] == 1:
            rewritten = dict(meta, target="auto/deploy-other")
            meta_path.write_text(json.dumps(rewritten), encoding="utf-8")
        return meta

    monkeypatch.setattr(loader, "_read_pending_meta", rewrite_after_first_read)

    with pytest.raises(PendingApprovalRefused) as refused:
        loader.approve_pending_update_checked("deploy-helper")

    assert refused.value.reason == "stale_base"
    assert other_md.read_text(encoding="utf-8") == other_before


def test_mutation_lock_registry_drops_idle_entries():
    from kiro_crew.skill_runtime import auto_skills

    class _Loader:
        _dir = "/tmp/lock-registry-probe"

    before = len(auto_skills._AUTO_MUTATION_LOCKS)
    for n in range(200):
        with auto_skills._auto_skill_mutation_lock(_Loader(), f"auto/skill-{n}"):
            with auto_skills._auto_skill_mutation_lock(_Loader(), f"skill-{n}"):
                assert auto_skills._holds_auto_skill_mutation_lock(_Loader(), f"skill-{n}")
        assert not auto_skills._holds_auto_skill_mutation_lock(_Loader(), f"skill-{n}")

    assert len(auto_skills._AUTO_MUTATION_LOCKS) == before


@pytest.mark.parametrize("shape", ["too_many", "too_large", "nested"])
def test_restage_refuses_helpers_over_the_script_caps(loader, monkeypatch, shape):
    from kiro_crew import skills as skills_mod

    _deploy_live(loader)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    sdir = loader._pending_root() / "deploy-helper" / "scripts"
    sdir.mkdir()
    if shape == "too_many":
        for index in range(skills_mod._PENDING_SCRIPT_MAX_ENTRIES + 1):
            (sdir / f"h{index}.py").write_text("print('ok')\n", encoding="utf-8")
    elif shape == "too_large":
        (sdir / "big.py").write_bytes(b"#" * (skills_mod.MAX_SCRIPT_BYTES + 1))
    else:
        (sdir / "deep").mkdir()
        (sdir / "deep" / "x.py").write_text("print('ok')\n", encoding="utf-8")

    candidate_dir = str(loader._pending_root() / "deploy-helper")
    real_walk = skills_mod.os.walk

    def no_eager_walk(top, *args, **kwargs):
        # Only the candidate's own tree is off limits: skill discovery walks the
        # skills root with os.walk on platforms without directory descriptors.
        if str(top).startswith(candidate_dir):
            raise AssertionError("restage must not materialise the helper tree with os.walk")
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(skills_mod.os, "walk", no_eager_walk)

    assert loader.restage_as_update("deploy-helper", "auto/deploy-live") is None
    assert sorted(item["slug"] for item in loader.list_pending_skills()) == ["deploy-helper"]


def test_restage_matches_a_repeat_by_source_not_by_a_shared_stem(loader):
    from kiro_crew import skills as skills_mod

    _deploy_live(loader)
    # Two canonical slugs that differ only past the characters the update stem keeps.
    keep = 63 - len("-update") - 3
    first_slug = "d" * keep + "-a"
    second_slug = "d" * keep + "-b"
    assert skills_mod._AUTO_NAME_PATTERN.match(first_slug)
    _pending(loader, first_slug, "deploy helper one", "deploy")
    _pending(loader, second_slug, "deploy helper two", "deploy")

    first = loader.restage_as_update(first_slug, "auto/deploy-live")
    second = loader.restage_as_update(second_slug, "auto/deploy-live")

    assert first is not None and second is not None
    assert first != second
    second_detail = loader.get_pending_skill(second.split("/", 1)[1])
    first_detail = loader.get_pending_skill(first.split("/", 1)[1])
    assert first_detail["meta"]["restaged_from"] == first_slug
    assert second_detail["meta"]["restaged_from"] == second_slug


def test_audit_reads_metadata_only_for_admitted_candidates(loader, monkeypatch):
    monkeypatch.setattr("kiro_crew.skills._AUDIT_MAX_ENTRIES", 2)
    for slug in ("c-cand", "a-cand", "e-cand", "b-cand"):
        _pending(loader, slug, "deploy a static website", "deploy website")
    read: list[str] = []
    original = loader._audit_pending_meta

    def spy(slug):
        read.append(slug)
        return original(slug)

    monkeypatch.setattr(loader, "_audit_pending_meta", spy)
    monkeypatch.setattr(
        loader, "list_pending_skills", lambda: pytest.fail("audit loaded the whole queue")
    )

    audit = loader.audit()

    assert read == ["a-cand", "b-cand"]
    assert audit["omitted_entries"] >= 2


def test_audit_skips_oversized_candidate_metadata(loader, monkeypatch):
    monkeypatch.setattr("kiro_crew.skills._AUDIT_MAX_META_BYTES", 16)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")

    assert loader._audit_pending_meta("deploy-helper") == {}


def _small_stat_and_sized_reads(monkeypatch, match):
    """Report a tiny size for files *match* selects, and record how much is read.

    Stands in for a file swapped for a large one after it was sized: any size
    check made by path sees the old file, the read sees the new one.
    """
    import os
    from pathlib import Path

    real_stat, real_lstat, real_open = Path.stat, Path.lstat, Path.open
    real_read_bytes, real_read_text = Path.read_bytes, Path.read_text
    reads: list[int] = []

    def small(st):
        fields = list(st[:10])
        fields[6] = 10
        return os.stat_result(fields)

    def stat(self, *args, **kwargs):
        st = real_stat(self, *args, **kwargs)
        return small(st) if match(self) else st

    def lstat(self):
        st = real_lstat(self)
        return small(st) if match(self) else st

    def sized_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if match(self):
            real_read = handle.read

            def read(size=-1):
                data = real_read(size)
                reads.append(len(data))
                return data

            handle.read = read
        return handle

    def read_bytes(self):
        if match(self):
            pytest.fail(f"unbounded read of {self}")
        return real_read_bytes(self)

    def read_text(self, *args, **kwargs):
        if match(self):
            pytest.fail(f"unbounded read of {self}")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(Path, "open", sized_open)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(Path, "read_text", read_text)
    return reads


def test_audit_caps_the_live_read_when_the_file_grows_after_its_stat(loader, monkeypatch):
    import kiro_crew.skills as skills_mod

    _deploy_live(loader)
    monkeypatch.setattr(skills_mod, "_AUDIT_MAX_META_BYTES", 2048)
    loader.create_skill(
        "grown-live", "---\nname: grown-live\ndescription: " + "deploy " * 1000 + "\n---\n"
    )
    reads = _small_stat_and_sized_reads(
        monkeypatch, lambda p: p.name == "SKILL.md" and p.parent.name == "grown-live"
    )

    result = loader.audit()

    names = {member["name"] for cluster in result["clusters"] for member in cluster["members"]}
    assert "grown-live" not in names
    assert result["omitted_entries"] == 1
    assert reads and max(reads) <= 2049


def test_audit_caps_the_pending_meta_read_when_it_grows_after_its_stat(loader, monkeypatch):
    monkeypatch.setattr("kiro_crew.skills._AUDIT_MAX_META_BYTES", 16)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    reads = _small_stat_and_sized_reads(
        monkeypatch,
        lambda p: p.name == ".meta.json" and p.parent.name == "deploy-helper",
    )

    assert loader._audit_pending_meta("deploy-helper") == {}
    assert reads and max(reads) <= 17


def test_audit_spec_states_the_production_thresholds():
    import re
    from pathlib import Path

    from kiro_crew import skills as skills_mod
    from kiro_crew.config.memory_sections import SkillsConfig

    spec = Path(__file__).resolve().parents[1] / "docs/system-specs/modules/memory-skills-hooks.md"
    line = next(
        ln for ln in spec.read_text(encoding="utf-8").splitlines() if "Queue-wide audit" in ln
    )
    stated = re.search(r"the default thresholds are ([0-9.]+) \([^)]*\) and ([0-9.]+)", line)

    assert stated is not None
    assert float(stated.group(1)) == SkillsConfig().auto_similarity_threshold
    assert float(stated.group(2)) == skills_mod._AUDIT_OVERLAP_THRESHOLD
