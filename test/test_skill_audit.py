"""Read-only skill overlap audit: loader pass, endpoint and payload bounds."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers import skills_audit as handlers
from kiro_crew.skill_runtime import audit as audit_mod
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
    def __init__(self, loader):
        self.app = {"state": SimpleNamespace(context_builder=SimpleNamespace(skills=loader))}
        self.match_info: dict[str, str] = {}

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


def _deploy_live(loader: SkillsLoader) -> None:
    assert loader.create_auto_skill(
        "deploy-live",
        description="deploy helper",
        triggers="deploy",
        procedure_md="## Steps\n\nRun it.",
        provenance=_provenance(),
    )


def _relations(loader: SkillsLoader) -> list[dict]:
    return [relation for cluster in loader.audit()["clusters"] for relation in cluster["relations"]]


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


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


def test_audit_never_pairs_a_builtin_with_another_live_skill(loader):
    from kiro_crew.skills import _PROVENANCE_MARKER

    _live(loader, "deploy-live", "deploy static service", "publish demo")
    _live(loader, "publish-live", "deploy static website", "ship preview")
    (loader._dir / "deploy-live" / _PROVENANCE_MARKER).write_text("{}", encoding="utf-8")

    assert loader.audit()["clusters"] == []


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


def test_audit_writes_nothing(loader):
    """The audit reports only: no candidate is staged and no skill file changes."""
    _deploy_live(loader)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    root = loader._dir
    before = _tree_snapshot(root)
    pending_before = [item["slug"] for item in loader.list_pending_skills()]

    assert loader.audit()["clusters"]

    assert _tree_snapshot(root) == before
    assert [item["slug"] for item in loader.list_pending_skills()] == pending_before


@pytest.mark.asyncio
async def test_audit_endpoint_returns_clusters(loader):
    _deploy_live(loader)
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
    assert "update_targets" not in cluster


def test_no_skill_route_stages_an_update_from_the_audit():
    """The audit is a GET only; there is no restage route or handler."""
    from aiohttp import web

    from kiro_crew.dashboard.routes import skills as skill_routes

    app = web.Application()
    skill_routes.register(app)
    audit_methods = set()
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter") or ""
        assert "restage" not in path
        if path == "/api/skills/-/audit":
            audit_methods.add(route.method)
    assert audit_methods <= {"GET", "HEAD"}
    assert "GET" in audit_methods
    assert not hasattr(handlers, "api_skill_pending_restage")


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
    cluster = {
        "classification": "duplicate",
        "score": 1.0,
        "members": members,
        "relations": relations,
    }

    bounded = handlers._bound_skill_audit_clusters([cluster] * 55)

    assert len(bounded) == handlers._SKILL_AUDIT_MAX_CLUSTERS
    first = bounded[0]
    assert len(first["members"]) == handlers._SKILL_AUDIT_MAX_MEMBERS
    assert len(first["relations"]) == handlers._SKILL_AUDIT_MAX_RELATIONS
    assert first["omitted_members"] == 5
    assert first["omitted_relations"] == 37
    returned_ids = {member["id"] for member in first["members"]}
    assert all(set(relation["members"]) <= returned_ids for relation in first["relations"])


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
    }
    bounded = handlers._bound_skill_audit_clusters([cluster])[0]
    ids = [member["id"] for member in bounded["members"]]
    assert ids[0] == "pending:z-late"
    assert len(bounded["relations"]) == 1 or bounded["omitted_relations"] == 1
    assert bounded["omitted_members"] == 6


def test_audit_bounds_admitted_entries_and_retained_relations(loader, monkeypatch):
    monkeypatch.setattr(audit_mod, "_AUDIT_MAX_ENTRIES", 3)
    monkeypatch.setattr(audit_mod, "_AUDIT_MAX_RELATIONS", 2)
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
    monkeypatch.setattr(audit_mod, "_AUDIT_MAX_TEXT_CHARS", 24)
    seen: list[str] = []
    original = audit_mod._words

    def capture(value: str):
        seen.append(value)
        return original(value)

    monkeypatch.setattr(audit_mod, "_words", capture)
    _pending(loader, "long-description", "x" * 200, "trigger")
    _live(loader, "long-live", "x" * 200, "trigger")
    loader.audit()
    assert seen
    assert all(len(value) <= 24 for value in seen)


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
    _live(loader, "deploy-live", "deploy a static website", "deploy website")
    _live(loader, "deploy-live-longer", "deploy a static website", "deploy website")
    monkeypatch.setattr(audit_mod, "_AUDIT_MAX_NAME_CHARS", len("deploy-live"))
    result = loader.audit()
    names = {member["name"] for cluster in result["clusters"] for member in cluster["members"]}
    assert "deploy-live-longer" not in names
    assert result["omitted_entries"] == 1


def test_audit_omits_an_oversized_live_skill_without_caching_it(loader, monkeypatch):
    _deploy_live(loader)
    monkeypatch.setattr(audit_mod, "_AUDIT_MAX_META_BYTES", 2048)
    loader.create_skill(
        "huge-live", "---\nname: huge-live\ndescription: " + "deploy " * 1000 + "\n---\n"
    )
    loader._fm_cache.clear()

    result = loader.audit()

    names = {member["name"] for cluster in result["clusters"] for member in cluster["members"]}
    assert "huge-live" not in names
    assert result["omitted_entries"] == 1
    assert loader._fm_cache == {}


def test_audit_reads_metadata_only_for_admitted_candidates(loader, monkeypatch):
    monkeypatch.setattr(audit_mod, "_AUDIT_MAX_ENTRIES", 2)
    for slug in ("c-cand", "a-cand", "e-cand", "b-cand"):
        _pending(loader, slug, "deploy a static website", "deploy website")
    read: list[str] = []
    original = loader._read_pending_meta

    def spy(slug, **kwargs):
        read.append(slug)
        return original(slug, **kwargs)

    monkeypatch.setattr(loader, "_read_pending_meta", spy)
    monkeypatch.setattr(
        loader, "list_pending_skills", lambda: pytest.fail("audit loaded the whole queue")
    )

    audit = loader.audit()

    assert read == ["a-cand", "b-cand"]
    assert audit["omitted_entries"] >= 2


def test_audit_skips_oversized_candidate_metadata(loader, monkeypatch):
    monkeypatch.setattr(audit_mod, "_AUDIT_MAX_META_BYTES", 16)
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    _live(loader, "deploy-live", "deploy helper", "deploy")

    # The oversized .meta.json is audited as having no description or triggers,
    # so the candidate pairs with nothing.
    assert loader._read_pending_meta("deploy-helper", max_bytes=16) == {}
    assert loader.audit()["clusters"] == []


def _small_stat_and_sized_reads(monkeypatch, match):
    """Report a tiny size for files *match* selects, and record how much is read.

    Stands in for a file swapped for a large one after it was sized: any size
    check made by path sees the old file, the read sees the new one.
    """
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
    _deploy_live(loader)
    monkeypatch.setattr(audit_mod, "_AUDIT_MAX_META_BYTES", 2048)
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
    _pending(loader, "deploy-helper", "deploy helper", "deploy")
    reads = _small_stat_and_sized_reads(
        monkeypatch,
        lambda p: p.name == ".meta.json" and p.parent.name == "deploy-helper",
    )

    assert loader._read_pending_meta("deploy-helper", max_bytes=16) == {}
    assert reads and max(reads) <= 17


def test_audit_spec_states_the_production_thresholds():
    import re

    from kiro_crew.config.memory_sections import SkillsConfig

    spec = Path(__file__).resolve().parents[1] / "docs/system-specs/modules/memory-skills-hooks.md"
    line = next(
        ln for ln in spec.read_text(encoding="utf-8").splitlines() if "Queue-wide audit" in ln
    )
    stated = re.search(r"the default thresholds are ([0-9.]+) \([^)]*\) and ([0-9.]+)", line)

    assert stated is not None
    assert float(stated.group(1)) == SkillsConfig().auto_similarity_threshold
    assert float(stated.group(2)) == audit_mod._AUDIT_OVERLAP_THRESHOLD
