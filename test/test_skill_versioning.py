"""Tests: pending skill UPDATES + per-skill version history.

Covers ``stage_skill_candidate(kind="update", ...)``, ``get_auto_skill_version``,
``read_auto_skill_body`` and ``approve_pending_update`` — the update-approval
flow that snapshots the current live version into ``auto/<slug>/.versions/``
before overwriting, and the guarantee that ``.versions`` never surfaces as a
loadable skill.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone

import pytest

import kiro_crew.skills as skills_mod
from kiro_crew.skills import MAX_SKILL_VERSIONS, AutoSkillProvenance, SkillsLoader


@pytest.fixture()
def loader(tmp_path):
    return SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)


def _prov(created_at: str = "") -> AutoSkillProvenance:
    return AutoSkillProvenance(
        session_key="s",
        created_at=created_at
        or datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    )


def _write_live(
    loader,
    slug,
    *,
    version=None,
    created_at="2020-01-01T00:00:00+00:00",
    body="original body",
):
    """Write a live auto-skill directly (optionally with a ``version`` line)."""
    live = loader._dir / "auto" / slug
    live.mkdir(parents=True, exist_ok=True)
    fm = [
        f"name: auto/{slug}",
        "description: live desc",
        "triggers: t",
        "source: auto",
        f"created_at: {created_at}",
    ]
    if version is not None:
        fm.append(f"version: {version}")
    content = "---\n" + "\n".join(fm) + "\n---\n\n# " + slug + "\n\n" + body + "\n"
    (live / "SKILL.md").write_text(content, encoding="utf-8")
    loader._invalidate_iter_cache()
    return live


def _stage_update(loader, slug, *, target, base_version=1, body="## Steps\n\nnew steps", scripts=None):
    return loader.stage_skill_candidate(
        slug,
        description=f"updated {slug}",
        triggers=slug,
        procedure_md=body,
        provenance=_prov(created_at="2099-12-31T00:00:00+00:00"),
        scripts=scripts,
        kind="update",
        target=target,
        base_version=base_version,
    )


# ── version reads ──

def test_get_auto_skill_version_defaults_to_one(loader):
    loader.create_auto_skill(
        "verd", description="d", triggers="t", procedure_md="body", provenance=_prov()
    )
    assert loader.get_auto_skill_version("auto/verd") == 1
    assert loader.get_auto_skill_version("verd") == 1  # bare slug accepted
    assert loader.get_auto_skill_version("auto/does-not-exist") == 1


def test_get_auto_skill_version_reads_frontmatter(loader):
    _write_live(loader, "verr", version=7)
    assert loader.get_auto_skill_version("auto/verr") == 7


def test_read_auto_skill_body_and_namespace_guard(loader):
    _write_live(loader, "bod", body="hello world")
    text = loader.read_auto_skill_body("auto/bod")
    assert text is not None and "hello world" in text
    assert loader.read_auto_skill_body("bod") is not None  # bare slug
    assert loader.read_auto_skill_body("auto/missing") is None
    # A non-auto namespace (multi-segment) is refused.
    assert loader.read_auto_skill_body("other/thing") is None


# ── staging update candidates ──

def test_staged_update_meta_appears_in_pending_list(loader):
    _write_live(loader, "greet")
    assert _stage_update(loader, "greet", target="auto/greet", base_version=1) == "auto/greet"
    entry = [p for p in loader.list_pending_skills() if p["slug"] == "greet"][0]
    assert entry["kind"] == "update"
    assert entry["target"] == "auto/greet"
    assert entry["base_version"] == 1
    detail = loader.get_pending_skill("greet")
    assert detail["kind"] == "update"
    assert detail["target"] == "auto/greet"
    assert detail["base_version"] == 1


# ── approve_pending_update happy path ──

def test_approve_update_carries_the_injection_opt_out_forward(loader):
    """A candidate never sets `inject_on_trigger`, so live must supply it.

    Without this the user's pointer-only choice is undone by an unrelated
    update approval — the skill silently starts injecting its whole body again.
    """
    live_dir = _write_live(loader, "quiet", body="v1 body")
    live = live_dir / "SKILL.md"
    live.write_text(
        live.read_text(encoding="utf-8").replace(
            "\n---\n", "\ninject_on_trigger: false\n---\n", 1
        ),
        encoding="utf-8",
    )
    loader._invalidate_iter_cache()
    assert loader.split_triggered(["auto/quiet"])[1] == ["auto/quiet"]

    _stage_update(loader, "quiet", target="auto/quiet", body="## Steps\n\nv2 steps")
    assert loader.approve_pending_update("quiet") == "auto/quiet"

    live_text = live.read_text(encoding="utf-8")
    assert "v2 steps" in live_text
    assert "inject_on_trigger: false" in live_text
    # And the runtime agrees, not just the file.
    assert loader.split_triggered(["auto/quiet"])[1] == ["auto/quiet"]


def test_approve_update_does_not_invent_an_opt_out(loader):
    _write_live(loader, "loud", body="v1 body")
    _stage_update(loader, "loud", target="auto/loud", body="## Steps\n\nv2 steps")
    assert loader.approve_pending_update("loud") == "auto/loud"

    live_text = (loader._dir / "auto" / "loud" / "SKILL.md").read_text(encoding="utf-8")
    assert "inject_on_trigger" not in live_text


def test_approve_update_snapshots_and_replaces(loader):
    _write_live(loader, "greet", created_at="2020-01-01T00:00:00+00:00", body="v1 body")
    _stage_update(loader, "greet", target="auto/greet", body="## Steps\n\nv2 steps")
    assert loader.approve_pending_update("greet") == "auto/greet"

    live = loader._dir / "auto" / "greet" / "SKILL.md"
    live_text = live.read_text(encoding="utf-8")
    # Live replaced with candidate body, version bumped, created_at preserved.
    assert "v2 steps" in live_text
    assert "v1 body" not in live_text
    assert loader.get_auto_skill_version("auto/greet") == 2
    assert "created_at: 2020-01-01T00:00:00+00:00" in live_text
    assert "name: auto/greet" in live_text

    # v1 snapshot captured the OLD live content.
    snap = loader._dir / "auto" / "greet" / ".versions" / "v1-SKILL.md"
    assert snap.exists()
    assert "v1 body" in snap.read_text(encoding="utf-8")

    # Pending gone; skill still loads + lists.
    assert loader.list_pending_skills() == []
    assert loader.load_skill("auto/greet") is not None
    assert [s["key"] for s in loader.list_auto_skills()] == ["auto/greet"]


def test_approve_update_moves_scripts_executable(loader):
    _write_live(loader, "withscript")
    _stage_update(
        loader,
        "withscript",
        target="auto/withscript",
        scripts=[{"filename": "run.py", "content": "print('ok')\n"}],
    )
    assert loader.approve_pending_update("withscript") == "auto/withscript"
    live_script = loader._dir / "auto" / "withscript" / "scripts" / "run.py"
    assert live_script.exists()
    if os.name != "nt":
        assert live_script.stat().st_mode & 0o111


# ── rejections ──

def test_approve_update_rejects_missing_target(loader):
    # target names a skill that is not live → refused, candidate intact.
    _stage_update(loader, "orphan", target="auto/nope")
    assert loader.approve_pending_update("orphan") is None
    assert any(p["slug"] == "orphan" for p in loader.list_pending_skills())
    assert not (loader._dir / "auto" / "nope").exists()


def test_approve_update_rejects_non_update_kind(loader):
    _write_live(loader, "plain")
    # A plain "new" candidate must not be approved via the update path.
    loader.stage_skill_candidate(
        "plain-cand",
        description="d",
        triggers="t",
        procedure_md="body",
        provenance=_prov(),
    )
    assert loader.approve_pending_update("plain-cand") is None
    assert any(p["slug"] == "plain-cand" for p in loader.list_pending_skills())


def test_approve_update_rejects_symlink(loader):
    _write_live(loader, "symk", version=1, body="untouched")
    _stage_update(loader, "symk", target="auto/symk")
    pdir = loader._pending_root() / "symk"
    (pdir / "scripts").mkdir(parents=True, exist_ok=True)
    target = pdir / "real.txt"
    target.write_text("ok", encoding="utf-8")
    os.symlink(str(target), str(pdir / "scripts" / "evil.py"))
    assert loader.approve_pending_update("symk") is None
    # Live untouched (still v1, original body), candidate still pending.
    assert loader.get_auto_skill_version("auto/symk") == 1
    assert "untouched" in (loader._dir / "auto" / "symk" / "SKILL.md").read_text()
    assert (loader._pending_root() / "symk").is_dir()


def test_failed_update_leaves_candidate_and_live_intact(loader, monkeypatch):
    _write_live(loader, "faux", version=1, body="live original")
    _stage_update(loader, "faux", target="auto/faux")
    # Redaction fails → abort before any live mutation.
    monkeypatch.setattr(loader, "_redact_file_in_place", lambda *a, **k: False)
    assert loader.approve_pending_update("faux") is None
    # Candidate intact.
    assert (loader._pending_root() / "faux" / "SKILL.md").exists()
    # Live untouched, no snapshot written.
    assert loader.get_auto_skill_version("auto/faux") == 1
    assert "live original" in (loader._dir / "auto" / "faux" / "SKILL.md").read_text()
    assert not (loader._dir / "auto" / "faux" / ".versions").exists()


# ── version pruning ──

def test_approve_update_prunes_versions_at_cap(loader):
    over = MAX_SKILL_VERSIONS + 5  # current live version
    _write_live(loader, "capped", version=over, body="current")
    vdir = loader._dir / "auto" / "capped" / ".versions"
    vdir.mkdir(parents=True, exist_ok=True)
    # Pre-populate v1 .. v(over-1) snapshots.
    for n in range(1, over):
        (vdir / f"v{n}-SKILL.md").write_text(f"snap {n}", encoding="utf-8")
    _stage_update(loader, "capped", target="auto/capped", base_version=over)
    assert loader.approve_pending_update("capped") == "auto/capped"
    # Approve wrote v<over> and pruned to the newest MAX_SKILL_VERSIONS.
    remaining = sorted(int(p.name[1:].split("-")[0]) for p in vdir.iterdir())
    assert len(remaining) == MAX_SKILL_VERSIONS
    assert remaining[0] == over - MAX_SKILL_VERSIONS + 1  # oldest survivor
    assert remaining[-1] == over  # newest snapshot present
    assert not (vdir / "v1-SKILL.md").exists()  # oldest pruned
    assert loader.get_auto_skill_version("auto/capped") == over + 1


# ── .versions never surfaces as a live skill ──

def test_versions_dir_absent_from_list_skills(loader):
    _write_live(loader, "shown", body="v1")
    _stage_update(loader, "shown", target="auto/shown", body="## Steps\n\nv2")
    assert loader.approve_pending_update("shown") == "auto/shown"
    # .versions/v1-SKILL.md exists on disk...
    assert (loader._dir / "auto" / "shown" / ".versions" / "v1-SKILL.md").exists()
    # ...but the dot-dir is pruned from discovery: no key references it.
    keys = [s["key"] for s in loader.list_skills()]
    assert keys == ["auto/shown"]
    assert not any(".versions" in k for k in keys)
    assert [s["key"] for s in loader.list_auto_skills()] == ["auto/shown"]


# ── Approval preview (Stage 6 review UI) ──────────────────────────────────────


def test_preview_returns_diff_and_versions(loader):
    _write_live(loader, "prev-one", body="OLD step")
    _stage_update(loader, "prev-one-update", target="auto/prev-one", body="## Steps\n\nNEW step")
    pv = loader.preview_pending_update("prev-one-update")
    assert pv is not None
    assert pv["from_version"] == 1
    assert pv["to_version"] == 2
    assert pv["stale_base"] is False
    # Unified diff shows the prose change on both sides.
    assert "-OLD step" in pv["diff"]
    assert "+NEW step" in pv["diff"]
    assert "prev-one" in pv["diff"]


def test_preview_proposed_body_matches_what_approve_writes(loader):
    """The preview must show the EXACT post-approval content, so the reviewer's
    diff is what approving does (frontmatter rewrite included)."""
    _write_live(loader, "prev-two", body="OLD")
    _stage_update(loader, "prev-two-update", target="auto/prev-two")
    proposed = loader.preview_pending_update("prev-two-update")["proposed_body"]
    assert loader.approve_pending_update("prev-two-update") == "auto/prev-two"
    live = (loader._dir / "auto" / "prev-two" / "SKILL.md").read_text(encoding="utf-8")
    assert live == proposed
    assert "version: 2" in live


def test_preview_flags_stale_base(loader):
    _write_live(loader, "prev-three", version=1, body="OLD")
    _stage_update(loader, "prev-three-update", target="auto/prev-three", base_version=99)
    pv = loader.preview_pending_update("prev-three-update")
    assert pv["stale_base"] is True
    assert pv["base_version"] == 99


def test_preview_rejects_non_update_and_missing_target(loader):
    # A plain new candidate has no preview.
    loader.stage_skill_candidate(
        "plain-cand",
        description="d",
        triggers="t",
        procedure_md="## Steps\n\nx",
        provenance=_prov(),
    )
    assert loader.preview_pending_update("plain-cand") is None
    # An update whose target was never live has no preview either.
    _stage_update(loader, "orphan-update", target="auto/does-not-exist")
    assert loader.preview_pending_update("orphan-update") is None
    # Unknown slug.
    assert loader.preview_pending_update("nope") is None


def test_preview_does_not_mutate_anything(loader):
    _write_live(loader, "prev-four", body="OLD")
    _stage_update(loader, "prev-four-update", target="auto/prev-four")
    live_path = loader._dir / "auto" / "prev-four" / "SKILL.md"
    cand = loader._pending_root() / "prev-four-update" / "SKILL.md"
    live_before = live_path.read_text(encoding="utf-8")
    cand_before = cand.read_text(encoding="utf-8")
    loader.preview_pending_update("prev-four-update")
    loader.preview_pending_update("prev-four-update")
    assert live_path.read_text(encoding="utf-8") == live_before
    assert cand.read_text(encoding="utf-8") == cand_before
    assert loader.get_auto_skill_version("auto/prev-four") == 1


def test_approve_update_script_promotion_failure_loses_nothing(loader, monkeypatch):
    """A failed script promotion must abort the approval, not silently drop the
    approved script. The pending dir is deleted on success, so a swallowed copy
    error would lose the script from BOTH the live skill and the queue."""
    _write_live(loader, "prom-fail", version=2, body="OLD")
    live_skill = loader._dir / "auto" / "prom-fail" / "SKILL.md"
    live_before = live_skill.read_text(encoding="utf-8")
    _stage_update(
        loader,
        "prom-fail-update",
        target="auto/prom-fail",
        base_version=2,
        scripts=[{"filename": "go.py", "content": "print('hi')\n"}],
    )
    cand_dir = loader._pending_root() / "prom-fail-update"

    real_copy = shutil.copy2

    def boom(src, dst, *a, **kw):
        if str(src).endswith("go.py"):
            raise OSError("read-only scripts dir")
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(shutil, "copy2", boom)
    assert loader.approve_pending_update("prom-fail-update") is None

    # Live skill untouched: still v2 with the old body, no half-promoted script.
    assert live_skill.read_text(encoding="utf-8") == live_before
    assert loader.get_auto_skill_version("auto/prom-fail") == 2
    assert not (loader._dir / "auto" / "prom-fail" / "scripts" / "go.py").exists()
    # Candidate (and its script) still reviewable — nothing was lost.
    assert (cand_dir / "SKILL.md").exists()
    assert (cand_dir / "scripts" / "go.py").exists()
    # The rolled-back snapshot is not left behind as a phantom version.
    vdir = loader._dir / "auto" / "prom-fail" / ".versions"
    assert not vdir.exists() or not list(vdir.iterdir())


def test_approve_update_promotes_scripts_on_success(loader):
    """The success path still lands the script live, executable on POSIX."""
    _write_live(loader, "prom-ok", version=1, body="OLD")
    _stage_update(
        loader,
        "prom-ok-update",
        target="auto/prom-ok",
        base_version=1,
        scripts=[{"filename": "go.py", "content": "print('hi')\n"}],
    )
    assert loader.approve_pending_update("prom-ok-update") == "auto/prom-ok"
    live_script = loader._dir / "auto" / "prom-ok" / "scripts" / "go.py"
    assert live_script.exists()
    if os.name != "nt":
        assert live_script.stat().st_mode & 0o111
    # Pending candidate consumed.
    assert not (loader._pending_root() / "prom-ok-update").exists()


def test_approve_update_preserves_pinned_flag(loader):
    """A pinned skill must stay pinned across an approved update — the pin is its
    lifecycle-archival exemption, so dropping it silently exposes the skill."""
    _write_live(loader, "pinned-skill", version=1, body="OLD")
    live_skill = loader._dir / "auto" / "pinned-skill" / "SKILL.md"
    assert loader.set_pinned("auto/pinned-skill", True) is True
    assert "pinned: true" in live_skill.read_text(encoding="utf-8")

    _stage_update(loader, "pinned-skill-update", target="auto/pinned-skill")
    # The preview must show the same content approve will write.
    proposed = loader.preview_pending_update("pinned-skill-update")["proposed_body"]
    assert loader.approve_pending_update("pinned-skill-update") == "auto/pinned-skill"

    body = live_skill.read_text(encoding="utf-8")
    assert "pinned: true" in body
    assert "version: 2" in body
    assert body == proposed
    # Exactly one pinned line (not duplicated by the rewrite).
    assert body.count("pinned:") == 1


def test_approve_update_does_not_invent_pinned_flag(loader):
    """An unpinned target must not become pinned by the rewrite."""
    _write_live(loader, "unpinned-skill", version=1, body="OLD")
    _stage_update(loader, "unpinned-skill-update", target="auto/unpinned-skill")
    assert loader.approve_pending_update("unpinned-skill-update") == "auto/unpinned-skill"
    body = (loader._dir / "auto" / "unpinned-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "pinned:" not in body


def test_approve_update_rollback_restores_overwritten_live_script(loader, monkeypatch):
    """Rollback must restore a PRE-EXISTING live script the promotion overwrote.
    Otherwise a later copy failure rolls SKILL.md back but leaves the replacement
    script live — an internally inconsistent skill."""
    _write_live(loader, "ow-skill", version=2, body="OLD")
    live_dir = loader._dir / "auto" / "ow-skill"
    live_scripts = live_dir / "scripts"
    live_scripts.mkdir(parents=True, exist_ok=True)
    old_script = live_scripts / "a.py"
    old_script.write_text("print('ORIGINAL')\n", encoding="utf-8")
    if os.name != "nt":
        old_script.chmod(0o755)
    old_mode = old_script.stat().st_mode
    live_before = (live_dir / "SKILL.md").read_text(encoding="utf-8")

    # Two scripts: a.py overwrites the existing one, b.py then fails.
    _stage_update(
        loader,
        "ow-skill-update",
        target="auto/ow-skill",
        base_version=2,
        scripts=[
            {"filename": "a.py", "content": "print('REPLACEMENT')\n"},
            {"filename": "b.py", "content": "print('second')\n"},
        ],
    )

    real_copy = shutil.copy2

    def boom(src, dst, *a, **kw):
        if str(src).endswith("b.py"):
            raise OSError("disk full")
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(shutil, "copy2", boom)
    assert loader.approve_pending_update("ow-skill-update") is None

    # The overwritten script is back to its original bytes and mode.
    assert old_script.read_text(encoding="utf-8") == "print('ORIGINAL')\n"
    if os.name != "nt":
        assert old_script.stat().st_mode == old_mode
    # The newly-created one is gone, and SKILL.md rolled back.
    assert not (live_scripts / "b.py").exists()
    assert (live_dir / "SKILL.md").read_text(encoding="utf-8") == live_before
    assert loader.get_auto_skill_version("auto/ow-skill") == 2
    # Candidate still reviewable.
    assert (loader._pending_root() / "ow-skill-update" / "SKILL.md").exists()


def test_refine_preserves_version_and_pinned(loader):
    """`update_auto_skill` (the auto-refine path) must not strip `version` or
    `pinned`. Dropping `version` makes the next update-approval read the skill as
    v1 and overwrite an existing v1 snapshot; dropping `pinned` removes the
    skill's lifecycle-archival exemption."""
    _write_live(loader, "refine-keep", version=3, body="OLD")
    live_skill = loader._dir / "auto" / "refine-keep" / "SKILL.md"
    assert loader.set_pinned("auto/refine-keep", True) is True

    assert loader.update_auto_skill(
        "auto/refine-keep",
        description="refined desc",
        triggers="t",
        procedure_md="## Steps\n\nrefined",
        provenance=_prov(created_at="2099-01-01T00:00:00+00:00"),
    ) is True

    body = live_skill.read_text(encoding="utf-8")
    assert "version: 3" in body
    assert "pinned: true" in body
    assert "refined" in body
    # created_at is still preserved (pre-existing behavior).
    assert "2020-01-01" in body
    assert loader.get_auto_skill_version("auto/refine-keep") == 3


def test_refine_preserves_the_injection_opt_out(loader):
    """Same class as version/pinned: the refine path rebuilds the frontmatter
    from the generator's template, which never emits `inject_on_trigger`. Losing
    it would silently restore full-body injection on a skill the user had made
    pointer-only."""
    _write_live(loader, "refine-quiet", body="OLD")
    live_skill = loader._dir / "auto" / "refine-quiet" / "SKILL.md"
    assert loader.set_inject_on_trigger("auto/refine-quiet", False) is True

    assert loader.update_auto_skill(
        "auto/refine-quiet",
        description="refined desc",
        triggers="t",
        procedure_md="## Steps\n\nrefined",
        provenance=_prov(),
    ) is True

    body = live_skill.read_text(encoding="utf-8")
    assert "refined" in body
    assert "inject_on_trigger: false" in body
    assert loader.split_triggered(["auto/refine-quiet"])[1] == ["auto/refine-quiet"]


def test_refine_does_not_invent_an_opt_out(loader):
    _write_live(loader, "refine-loud", body="OLD")
    assert loader.update_auto_skill(
        "auto/refine-loud",
        description="d",
        triggers="t",
        procedure_md="## Steps\n\nrefined",
        provenance=_prov(),
    ) is True
    body = (loader._dir / "auto" / "refine-loud" / "SKILL.md").read_text(encoding="utf-8")
    assert "inject_on_trigger" not in body


def test_approve_update_never_clobbers_an_existing_snapshot(loader):
    """If version numbering has drifted so the live skill reads as an older
    version, the snapshot must continue ABOVE the highest existing one rather
    than destroying it."""
    _write_live(loader, "drift", version=1, body="ORIGINAL-V1")
    # Simulate history from a prior approval whose version line was later lost.
    vdir = loader._dir / "auto" / "drift" / ".versions"
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "v1-SKILL.md").write_text("SNAPSHOT-OF-ORIGINAL-V1\n", encoding="utf-8")

    _stage_update(loader, "drift-update", target="auto/drift", base_version=1)
    assert loader.approve_pending_update("drift-update") == "auto/drift"

    # The pre-existing v1 snapshot is intact...
    assert (vdir / "v1-SKILL.md").read_text(encoding="utf-8") == "SNAPSHOT-OF-ORIGINAL-V1\n"
    # ...and the current body was snapshotted under a fresh number instead.
    assert (vdir / "v2-SKILL.md").exists()
    assert "ORIGINAL-V1" in (vdir / "v2-SKILL.md").read_text(encoding="utf-8")
    assert loader.get_auto_skill_version("auto/drift") == 3


def test_approve_update_rejects_symlinked_live_scripts_dir(loader, tmp_path):
    """A symlinked live `scripts/` would let promotion write candidate content
    outside the skill directory — refuse before any mutation."""
    if os.name == "nt":
        pytest.skip("symlink creation needs privileges on Windows")
    _write_live(loader, "sym-live", version=1, body="OLD")
    live_dir = loader._dir / "auto" / "sym-live"
    outside = tmp_path / "outside"
    outside.mkdir()
    (live_dir / "scripts").symlink_to(outside, target_is_directory=True)
    live_before = (live_dir / "SKILL.md").read_text(encoding="utf-8")

    _stage_update(
        loader,
        "sym-live-update",
        target="auto/sym-live",
        base_version=1,
        scripts=[{"filename": "go.py", "content": "print('hi')\n"}],
    )
    assert loader.approve_pending_update("sym-live-update") is None
    # Nothing written outside, nothing changed live, candidate intact.
    assert list(outside.iterdir()) == []
    assert (live_dir / "SKILL.md").read_text(encoding="utf-8") == live_before
    assert (loader._pending_root() / "sym-live-update" / "SKILL.md").exists()


def test_read_auto_skill_body_refuses_symlinked_skill_file(loader, tmp_path):
    """The live body is fed to the merge turn UNREDACTED, so a swapped SKILL.md
    symlink pointing at credential storage would put those bytes into an LLM
    prompt. The read must refuse rather than follow the link."""
    if os.name == "nt":
        pytest.skip("symlink creation needs privileges on Windows")
    secret = tmp_path / "credentials"
    secret.write_text("aws_secret_access_key = SHOULD-NEVER-BE-READ\n", encoding="utf-8")
    live_dir = loader._dir / "auto" / "sym-read"
    live_dir.mkdir(parents=True, exist_ok=True)
    (live_dir / "SKILL.md").symlink_to(secret)

    assert loader.read_auto_skill_body("auto/sym-read") is None


def test_read_auto_skill_body_refuses_symlinked_skill_dir(loader, tmp_path):
    """Same guard when the skill DIRECTORY itself is the symlink."""
    if os.name == "nt":
        pytest.skip("symlink creation needs privileges on Windows")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: auto/x\n---\n\nleaked\n", encoding="utf-8")
    (loader._dir / "auto").mkdir(parents=True, exist_ok=True)
    (loader._dir / "auto" / "sym-dir").symlink_to(outside, target_is_directory=True)

    assert loader.read_auto_skill_body("auto/sym-dir") is None


def test_preview_pending_update_refuses_symlinked_live_body(loader, tmp_path):
    """The preview feeds the dashboard API — it must use the same guarded read."""
    if os.name == "nt":
        pytest.skip("symlink creation needs privileges on Windows")
    secret = tmp_path / "credentials"
    secret.write_text("aws_secret_access_key = SHOULD-NEVER-BE-READ\n", encoding="utf-8")
    live_dir = loader._dir / "auto" / "sym-prev"
    live_dir.mkdir(parents=True, exist_ok=True)
    (live_dir / "SKILL.md").symlink_to(secret)

    _stage_update(loader, "sym-prev-update", target="auto/sym-prev")
    assert loader.preview_pending_update("sym-prev-update") is None


def test_read_auto_skill_body_still_reads_a_normal_skill(loader):
    """The guard must not break the ordinary path."""
    _write_live(loader, "plain-read", version=2, body="REAL BODY")
    body = loader.read_auto_skill_body("auto/plain-read")
    assert body is not None and "REAL BODY" in body


def test_approve_update_rejects_a_stale_base(loader):
    """Two updates staged at v1; approving the first moves the skill to v2. The
    second was merged from v1 prose, so applying it would replace the changes just
    approved — it must be refused, not merely warned about."""
    _write_live(loader, "race", version=1, body="ORIGINAL")
    _stage_update(loader, "race-a", target="auto/race", base_version=1, body="## Steps\n\nFROM-A")
    _stage_update(loader, "race-b", target="auto/race", base_version=1, body="## Steps\n\nFROM-B")

    assert loader.approve_pending_update("race-a") == "auto/race"
    live = loader._dir / "auto" / "race" / "SKILL.md"
    assert "FROM-A" in live.read_text(encoding="utf-8")
    assert loader.get_auto_skill_version("auto/race") == 2

    # The second is now stale -> refused, live untouched, candidate still pending.
    assert loader.approve_pending_update("race-b") is None
    body = live.read_text(encoding="utf-8")
    assert "FROM-A" in body and "FROM-B" not in body
    assert loader.get_auto_skill_version("auto/race") == 2
    assert (loader._pending_root() / "race-b" / "SKILL.md").exists()


def test_approve_update_allows_a_candidate_without_base_version(loader):
    """Backward compat: a candidate staged before base_version existed has no
    recorded base, so the staleness gate must not block it."""
    _write_live(loader, "nobase", version=2, body="OLD")
    name = loader.stage_skill_candidate(
        "nobase-update",
        description="d",
        triggers="t",
        procedure_md="## Steps\n\nNEW",
        provenance=_prov(),
        kind="update",
        target="auto/nobase",
    )
    assert name is not None
    meta_path = loader._pending_root() / "nobase-update" / ".meta.json"
    import json as _json

    assert "base_version" not in _json.loads(meta_path.read_text(encoding="utf-8"))
    assert loader.approve_pending_update("nobase-update") == "auto/nobase"
    assert "NEW" in (loader._dir / "auto" / "nobase" / "SKILL.md").read_text(encoding="utf-8")


def test_approve_update_matching_base_still_succeeds(loader):
    """The gate must not block the normal (in-sync) case."""
    _write_live(loader, "insync", version=4, body="OLD")
    _stage_update(loader, "insync-update", target="auto/insync", base_version=4)
    assert loader.approve_pending_update("insync-update") == "auto/insync"
    assert loader.get_auto_skill_version("auto/insync") == 5


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_read_auto_skill_body_reads_the_validated_path_not_the_original(tmp_path, monkeypatch):
    """The guards vet the RESOLVED path, so the read must use that same path.

    Reading the original path again would validate one path and read another —
    a swap of the final component between check and read would put the
    substituted bytes into the update-merge prompt.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    loader = SkillsLoader()
    live = tmp_path / "skills" / "auto" / "deploy-x"
    live.mkdir(parents=True)
    (live / "SKILL.md").write_text("---\nname: auto/deploy-x\n---\n\nbody\n", encoding="utf-8")

    seen: list[str] = []
    real_safe_read = skills_mod.safe_read_file

    def spy(path: str) -> str:
        seen.append(path)
        return real_safe_read(path)

    monkeypatch.setattr(skills_mod, "safe_read_file", spy)
    assert loader.read_auto_skill_body("auto/deploy-x") == (
        "---\nname: auto/deploy-x\n---\n\nbody\n"
    )
    # Routed through the hardened primitive, using the canonical path.
    assert seen == [os.path.realpath(str(live / "SKILL.md"))]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_read_auto_skill_body_returns_none_when_safe_read_refuses(tmp_path, monkeypatch):
    """A PermissionError from the hardened reader (sensitive path or a detected
    symlink swap) must surface as None, not propagate into the merge path."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    loader = SkillsLoader()
    live = tmp_path / "skills" / "auto" / "deploy-y"
    live.mkdir(parents=True)
    (live / "SKILL.md").write_text("body", encoding="utf-8")

    def refuse(path: str) -> str:
        raise PermissionError("Blocked: refusing to follow symlink")

    monkeypatch.setattr(skills_mod, "safe_read_file", refuse)
    assert loader.read_auto_skill_body("auto/deploy-y") is None


def test_stale_rejection_leaves_the_candidate_unredacted(loader):
    """A stale rejection keeps the candidate PENDING so it can be dismissed — so it
    must also leave it byte-identical to what was staged.

    Redaction runs in place before the staleness gate. Without a restore, the
    rejected draft is left permanently altered: the reviewer re-opens it and sees
    placeholder text instead of what was staged, on a candidate the system claims
    it did not touch.
    """
    secret = "AKIAIOSFODNN7EXAMPLE"
    _write_live(loader, "redact", version=1, body="ORIGINAL")
    _stage_update(
        loader,
        "redact-a",
        target="auto/redact",
        base_version=1,
        body="## Steps\n\nFROM-A",
    )
    _stage_update(
        loader,
        "redact-b",
        target="auto/redact",
        base_version=1,
        body=f"## Steps\n\nuse key {secret} here",
    )
    candidate = loader._pending_root() / "redact-b" / "SKILL.md"
    before = candidate.read_bytes()
    assert secret.encode() in before

    # Advance live so redact-b becomes stale.
    assert loader.approve_pending_update("redact-a") == "auto/redact"
    assert loader.approve_pending_update("redact-b") is None

    # Still pending, and byte-identical to what was staged.
    assert candidate.exists()
    assert candidate.read_bytes() == before
