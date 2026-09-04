"""Phase-0 tests: auto-skill lifecycle (archive-not-delete, pin/cron exempt,
grace floor, max-N backstop) and archive dir pruning from discovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kiro_crew.skills import AutoSkillProvenance, SkillsLoader


def _iso(days_ago: float) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _mk(loader: SkillsLoader, slug: str, *, created_days_ago: float, pinned: bool = False) -> str:
    prov = AutoSkillProvenance(session_key="s", created_at=_iso(created_days_ago), pinned=pinned)
    name = loader.create_auto_skill(
        slug,
        description=f"desc for {slug}",
        triggers=slug,
        procedure_md="## Steps\n\ndo the thing",
        provenance=prov,
    )
    assert name == f"auto/{slug}"
    return name


@pytest.fixture()
def loader(tmp_path):
    return SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)


def _run(loader: SkillsLoader, **kw):
    defaults = dict(max_auto_skills=100, stale_after_days=30, archive_after_days=90)
    defaults.update(kw)
    return loader.run_skill_lifecycle(**defaults)


def test_old_unused_skill_is_archived_not_deleted(loader):
    _mk(loader, "old-unused", created_days_ago=200)
    counts = _run(loader)
    assert counts["archived"] == 1
    # gone from live set...
    assert loader.list_auto_skills() == []
    # ...but recoverable in the archive.
    archived = loader.list_archived_auto_skills()
    assert [a["slug"] for a in archived] == ["old-unused"]


def test_never_used_recent_skill_survives_grace_floor(loader):
    _mk(loader, "fresh", created_days_ago=1)
    counts = _run(loader)
    assert counts["archived"] == 0
    assert [s["key"] for s in loader.list_auto_skills()] == ["auto/fresh"]


def test_recently_used_old_skill_survives(loader):
    _mk(loader, "used-old", created_days_ago=200)
    # Record a use → last_seen=now becomes the anchor, beating the old created_at.
    assert loader._usage is not None
    loader._usage.record("auto/used-old")
    counts = _run(loader)
    assert counts["archived"] == 0
    assert [s["key"] for s in loader.list_auto_skills()] == ["auto/used-old"]


def test_pinned_skill_is_exempt(loader):
    _mk(loader, "pinned-old", created_days_ago=200, pinned=True)
    counts = _run(loader)
    assert counts["archived"] == 0
    assert [s["key"] for s in loader.list_auto_skills()] == ["auto/pinned-old"]


def test_cron_referenced_skill_is_exempt(loader):
    _mk(loader, "cron-old", created_days_ago=200)
    counts = _run(loader, cron_referenced={"auto/cron-old"})
    assert counts["archived"] == 0
    assert [s["key"] for s in loader.list_auto_skills()] == ["auto/cron-old"]


def test_max_n_backstop_archives_lowest_ranked(loader):
    # Three recent, never-archived-by-age skills; cap at 2 → 1 archived.
    for slug in ("aaa", "bbb", "ccc"):
        _mk(loader, slug, created_days_ago=1)
    # Make 'aaa' hot so it survives the cap; bbb/ccc stay at 0 hits.
    loader._usage.record("auto/aaa")
    counts = _run(loader, max_auto_skills=2)
    assert counts["capped"] == 1
    live = {s["key"] for s in loader.list_auto_skills()}
    assert "auto/aaa" in live
    assert len(live) == 2


def test_pin_unpin_roundtrip(loader):
    name = _mk(loader, "toggle", created_days_ago=1)
    assert loader.set_pinned(name, True) is True
    meta = loader._cached_frontmatter  # noqa: F841 (ensure attr exists)
    import pathlib

    fm = loader._cached_frontmatter(pathlib.Path(loader._dir / name / "SKILL.md"), within=None)
    assert str(fm.get("pinned", "")).lower() == "true"
    assert loader.set_pinned(name, False) is True
    fm2 = loader._cached_frontmatter(pathlib.Path(loader._dir / name / "SKILL.md"), within=None)
    assert "pinned" not in fm2


def test_restore_roundtrip(loader):
    _mk(loader, "restore-me", created_days_ago=200)
    _run(loader)
    assert loader.list_auto_skills() == []
    restored = loader.restore_auto_skill("restore-me")
    assert restored == "auto/restore-me"
    assert [s["key"] for s in loader.list_auto_skills()] == ["auto/restore-me"]


def test_set_pinned_refuses_non_auto(loader):
    loader.create_skill("hand/authored", "---\nname: hand/authored\n---\nbody")
    assert loader.set_pinned("hand/authored", True) is False


def test_pin_write_failure_preserves_skill(loader, monkeypatch):
    """A pin write failure (e.g. full disk) must NOT truncate the live SKILL.md —
    atomic_write leaves the original content intact on error."""
    import pathlib

    import kiro_crew.skills as S

    name = _mk(loader, "keepme", created_days_ago=1)
    skill_file = pathlib.Path(loader._dir / name / "SKILL.md")
    before = skill_file.read_text(encoding="utf-8")

    def _boom(*a, **k):
        raise OSError("no space left on device")

    monkeypatch.setattr(S, "atomic_write", _boom)
    with pytest.raises(OSError):
        loader.set_pinned(name, True)
    assert skill_file.read_text(encoding="utf-8") == before  # untouched, not truncated


def test_archive_refuses_non_auto(loader):
    loader.create_skill("hand/authored", "---\nname: hand/authored\n---\nbody")
    assert loader.archive_auto_skill("hand/authored") is False


def test_archive_collision_preserves_prior_archive(loader):
    """Archiving a re-created same-slug skill must not destroy the earlier
    archived copy (GPT HIGH: version the dest instead of rmtree)."""
    _mk(loader, "dup-skill", created_days_ago=1)
    assert loader.archive_auto_skill("auto/dup-skill") is True
    # Re-create the slug (its live path is now free) and archive again.
    _mk(loader, "dup-skill", created_days_ago=0)
    assert loader.archive_auto_skill("auto/dup-skill") is True
    slugs = {a["slug"] for a in loader.list_archived_auto_skills()}
    # Both copies survive — the original was not clobbered.
    assert "dup-skill" in slugs
    assert "dup-skill-2" in slugs


def test_lifecycle_exempts_named_skill_from_cap(loader):
    """A skill named in `exempt` survives the max-N backstop even when it is
    brand-new and zero-hit (GPT MEDIUM: approve must not evict its own skill)."""
    for i in range(3):
        _mk(loader, f"cap-{i}", created_days_ago=0)
    res = _run(loader, max_auto_skills=2, exempt={"auto/cap-0"})
    live = {s["key"] for s in loader.list_auto_skills()}
    assert "auto/cap-0" in live
    assert res["capped"] >= 1
