"""Tests: pending skill UPDATES + per-skill version history.

Covers ``stage_skill_candidate(kind="update", ...)``, ``get_auto_skill_version``,
``read_auto_skill_body`` and ``approve_pending_update`` — the update-approval
flow that snapshots the current live version into ``auto/<slug>/.versions/``
before overwriting, and the guarantee that ``.versions`` never surfaces as a
loadable skill.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

import kiro_crew.skills as skills_mod
from kiro_crew.skills import MAX_SKILL_VERSIONS, AutoSkillProvenance, SkillsLoader


@pytest.fixture()
def loader():
    return SkillsLoader(install_builtins=False)


def _prov(created_at: str = "") -> AutoSkillProvenance:
    return AutoSkillProvenance(
        session_key="s",
        created_at=created_at or datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
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


def _stage_update(
    loader,
    slug,
    *,
    target,
    base_version=1,
    body="## Steps\n\nnew steps",
    scripts=None,
    notify=True,
    stage_token=None,
    base_content_hash=None,
    unattended_binding_out=None,
):
    if base_content_hash is None and stage_token is not None:
        live_body = loader.read_auto_skill_body(target)
        assert live_body is not None
        base_content_hash = hashlib.sha256(live_body.encode("utf-8")).hexdigest()
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
        notify=notify,
        stage_token=stage_token,
        base_content_hash=base_content_hash,
        unattended_binding_out=unattended_binding_out,
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
        live.read_text(encoding="utf-8").replace("\n---\n", "\ninject_on_trigger: false\n---\n", 1),
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


def test_new_skill_approval_rejects_claimed_update_kind(loader):
    live = _write_live(loader, "stale-route", version=1, body="live original")
    _stage_update(
        loader,
        "stale-route-update",
        target="auto/stale-route",
        body="## Steps\n\nupdated body",
    )
    pending = loader._pending_root() / "stale-route-update"
    candidate_before = (pending / "SKILL.md").read_bytes()
    metadata_before = (pending / ".meta.json").read_bytes()
    live_before = (live / "SKILL.md").read_bytes()

    # Simulate a dashboard request routed as "new" from stale pre-claim metadata.
    assert loader.approve_pending_skill("stale-route-update") is None

    assert (live / "SKILL.md").read_bytes() == live_before
    assert not (loader._dir / "auto" / "stale-route-update").exists()
    assert (pending / "SKILL.md").read_bytes() == candidate_before
    assert (pending / ".meta.json").read_bytes() == metadata_before


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

    assert (
        loader.update_auto_skill(
            "auto/refine-keep",
            description="refined desc",
            triggers="t",
            procedure_md="## Steps\n\nrefined",
            provenance=_prov(created_at="2099-01-01T00:00:00+00:00"),
        )
        is True
    )

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

    assert (
        loader.update_auto_skill(
            "auto/refine-quiet",
            description="refined desc",
            triggers="t",
            procedure_md="## Steps\n\nrefined",
            provenance=_prov(),
        )
        is True
    )

    body = live_skill.read_text(encoding="utf-8")
    assert "refined" in body
    assert "inject_on_trigger: false" in body
    assert loader.split_triggered(["auto/refine-quiet"])[1] == ["auto/refine-quiet"]


def test_refine_does_not_invent_an_opt_out(loader):
    _write_live(loader, "refine-loud", body="OLD")
    assert (
        loader.update_auto_skill(
            "auto/refine-loud",
            description="d",
            triggers="t",
            procedure_md="## Steps\n\nrefined",
            provenance=_prov(),
        )
        is True
    )
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


# ── unattended update promotion / atomic claim ──


def test_claim_requires_resolved_private_root_to_be_sensitive(loader, monkeypatch, tmp_path):
    _write_live(loader, "alias-guard", version=1, body="OLD")
    _stage_update(loader, "alias-guard-update", target="auto/alias-guard")
    private_root = loader._private_root()
    physical_alias = tmp_path / "project" / "crew-data" / "skills" / "auto" / ".private"
    real_resolve = Path.resolve

    def resolve_private_root(path, *args, **kwargs):
        if path == private_root:
            return physical_alias
        return real_resolve(path, *args, **kwargs)

    checked: list[str] = []

    def sensitive(path: str) -> bool:
        checked.append(path)
        return path == str(private_root)

    monkeypatch.setattr(Path, "resolve", resolve_private_root)
    monkeypatch.setattr(skills_mod, "is_sensitive_path", sensitive)

    assert loader._claim_pending_update("alias-guard-update") is None
    assert checked == [str(private_root), str(physical_alias)]
    assert (loader._pending_root() / "alias-guard-update" / "SKILL.md").exists()


@pytest.mark.parametrize("root_kind", ["private", "claims", "locks"])
def test_claim_rejects_linked_private_state_roots(loader, tmp_path, root_kind):
    _write_live(loader, "linked-private-root", version=1, body="OLD")
    _stage_update(
        loader,
        f"linked-{root_kind}-root-update",
        target="auto/linked-private-root",
    )
    private_root = loader._private_root()
    claims_root = loader._claims_root()
    locks_root = loader._locks_root()
    if root_kind == "private":
        link = private_root
        shutil.rmtree(private_root)
    elif root_kind == "claims":
        link = claims_root
        claims_root.rmdir()
    else:
        link = locks_root
        (locks_root / "pending.lock").unlink()
        (locks_root / "claims").rmdir()
        locks_root.rmdir()
    outside = tmp_path / f"outside-{root_kind}"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("DO NOT TOUCH", encoding="utf-8")
    skills_mod.platform_compat.symlink_or_junction(outside, link)

    assert loader._claim_pending_update(f"linked-{root_kind}-root-update") is None

    assert sentinel.read_text(encoding="utf-8") == "DO NOT TOUCH"
    assert sorted(child.name for child in outside.iterdir()) == ["sentinel"]
    assert (loader._pending_root() / f"linked-{root_kind}-root-update" / "SKILL.md").exists()


def test_auto_apply_prose_update_snapshots_and_notifies(loader):
    _write_live(loader, "auto-prose", version=2, body="OLD")
    token = "a" * 32
    binding: list[str] = []
    _stage_update(
        loader,
        "auto-prose-update",
        target="auto/auto-prose",
        base_version=2,
        notify=False,
        stage_token=token,
        unattended_binding_out=binding,
    )
    seen: list[dict] = []
    skills_mod.set_update_auto_applied_hook(seen.append)
    try:
        assert loader.auto_apply_pending_update(
            "auto-prose-update",
            expected_stage_token=token,
            expected_candidate_binding=binding[0],
        ) == ("auto/auto-prose", 3)
    finally:
        skills_mod.set_update_auto_applied_hook(None)

    assert loader.get_auto_skill_version("auto/auto-prose") == 3
    assert (loader._dir / "auto" / "auto-prose" / ".versions" / "v2-SKILL.md").exists()
    assert not (loader._pending_root() / "auto-prose-update").exists()
    assert seen[0]["new_version"] == 3


def test_auto_apply_binding_uses_exact_staged_bytes(loader, monkeypatch):
    _write_live(loader, "windows-newlines", version=1, body="OLD")
    real_write_text = Path.write_text

    def write_with_windows_newlines(path, data, *args, **kwargs):
        candidate = Path(path)
        if candidate.name == "SKILL.md" and loader._pending_root() in candidate.parents:
            return candidate.write_bytes(data.replace("\n", "\r\n").encode("utf-8"))
        return real_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_with_windows_newlines)
    token = "9" * 32
    binding: list[str] = []
    _stage_update(
        loader,
        "windows-newlines-update",
        target="auto/windows-newlines",
        notify=False,
        stage_token=token,
        unattended_binding_out=binding,
    )
    staged = loader._pending_root() / "windows-newlines-update" / "SKILL.md"
    assert b"\r\n" in staged.read_bytes()

    assert loader.auto_apply_pending_update(
        "windows-newlines-update",
        expected_stage_token=token,
        expected_candidate_binding=binding[0],
    ) == ("auto/windows-newlines", 2)


def test_auto_apply_refuses_concurrent_dashboard_edit(loader, monkeypatch):
    live_dir = _write_live(loader, "dashboard-race", version=1, body="OLD")
    live_file = live_dir / "SKILL.md"
    token = "e" * 32
    binding: list[str] = []
    _stage_update(
        loader,
        "dashboard-race-update",
        target="auto/dashboard-race",
        base_version=1,
        notify=False,
        stage_token=token,
        unattended_binding_out=binding,
    )
    dashboard_content = live_file.read_text(encoding="utf-8").replace("OLD", "DASHBOARD EDIT")

    writer_has_lock = threading.Event()
    allow_writer = threading.Event()
    auto_lock_attempted = threading.Event()
    real_write = loader._update_skill_unlocked
    real_lock = loader._promotion_lock

    def blocked_dashboard_write(name, content):
        writer_has_lock.set()
        assert allow_writer.wait(timeout=5)
        return real_write(name, content)

    @contextlib.contextmanager
    def observed_lock(slug):
        if threading.current_thread().name.startswith("auto-apply"):
            auto_lock_attempted.set()
        with real_lock(slug) as acquired:
            yield acquired

    monkeypatch.setattr(loader, "_update_skill_unlocked", blocked_dashboard_write)
    monkeypatch.setattr(loader, "_promotion_lock", observed_lock)

    with (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="dashboard-edit") as edit_pool,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="auto-apply") as apply_pool,
    ):
        edit_future = edit_pool.submit(
            loader.update_skill, "auto/dashboard-race", dashboard_content
        )
        assert writer_has_lock.wait(timeout=5)
        try:
            apply_future = apply_pool.submit(
                loader.auto_apply_pending_update,
                "dashboard-race-update",
                expected_stage_token=token,
                expected_candidate_binding=binding[0],
            )
            assert auto_lock_attempted.wait(timeout=5)
            assert not apply_future.done()
        finally:
            allow_writer.set()
        assert edit_future.result(timeout=5) is True
        assert apply_future.result(timeout=5) is None

    assert live_file.read_text(encoding="utf-8") == dashboard_content
    assert (loader._pending_root() / "dashboard-race-update" / "SKILL.md").exists()
    assert not (live_dir / ".versions").exists()


@pytest.mark.parametrize(
    ("method_name", "helper_name", "extra_args"),
    [
        ("update_skill", "_update_skill_unlocked", ("dashboard body",)),
        ("delete_skill", "_delete_skill_unlocked", ()),
        ("set_pinned", "_set_pinned_unlocked", (True,)),
        ("set_inject_on_trigger", "_set_inject_on_trigger_unlocked", (False,)),
        ("archive_auto_skill", "_archive_auto_skill_unlocked", ()),
    ],
)
def test_live_auto_mutators_share_promotion_lock(
    loader, monkeypatch, method_name, helper_name, extra_args
):
    _write_live(loader, "mutation-lock", version=1, body="OLD")
    name = "auto/mutation-lock"
    attempted = threading.Event()
    mutation_entered = threading.Event()
    real_lock = loader._promotion_lock
    real_mutation = getattr(loader, helper_name)

    @contextlib.contextmanager
    def observed_lock(slug):
        if threading.current_thread().name.startswith("live-mutator"):
            attempted.set()
        with real_lock(slug) as acquired:
            yield acquired

    def observed_mutation(*args, **kwargs):
        mutation_entered.set()
        return real_mutation(*args, **kwargs)

    monkeypatch.setattr(loader, "_promotion_lock", observed_lock)
    monkeypatch.setattr(loader, helper_name, observed_mutation)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-mutator") as pool:
        with real_lock("mutation-lock") as acquired:
            assert acquired is True
            future = pool.submit(getattr(loader, method_name), name, *extra_args)
            assert attempted.wait(timeout=5)
            assert not mutation_entered.is_set()
        assert future.result(timeout=5) is True
    assert mutation_entered.is_set()


@pytest.mark.parametrize("name", ["AUTO/mutation-lock", "Auto/mutation-lock", "aUtO/mutation-lock"])
def test_live_auto_mutator_refuses_noncanonical_namespace_alias(loader, monkeypatch, name):
    def unexpected_write(_name, _content):
        pytest.fail("noncanonical auto namespace reached the unlocked writer")

    monkeypatch.setattr(loader, "_update_skill_unlocked", unexpected_write)

    assert loader.update_skill(name, "dashboard body") is False


def test_auto_apply_refuses_physical_scripts_and_restores_review(loader):
    _write_live(loader, "physical", version=1, body="OLD")
    token = "b" * 32
    binding: list[str] = []
    _stage_update(
        loader,
        "physical-update",
        target="auto/physical",
        notify=False,
        stage_token=token,
        unattended_binding_out=binding,
    )
    script_dir = loader._pending_root() / "physical-update" / "scripts"
    script_dir.mkdir()
    (script_dir / "late.py").write_text("print('late')\n", encoding="utf-8")

    assert (
        loader.auto_apply_pending_update(
            "physical-update",
            expected_stage_token=token,
            expected_candidate_binding=binding[0],
        )
        is None
    )
    assert (loader._pending_root() / "physical-update" / "scripts" / "late.py").exists()
    assert loader.get_auto_skill_version("auto/physical") == 1


def test_auto_apply_refuses_stage_token_swap(loader):
    _write_live(loader, "tokened", version=1, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        "tokened-update",
        target="auto/tokened",
        notify=False,
        stage_token="original",
        unattended_binding_out=binding,
    )
    assert (
        loader.auto_apply_pending_update(
            "tokened-update",
            expected_stage_token="replacement",
            expected_candidate_binding=binding[0],
        )
        is None
    )
    assert (loader._pending_root() / "tokened-update" / "SKILL.md").exists()
    assert loader.get_auto_skill_version("auto/tokened") == 1


def test_auto_apply_refuses_candidate_and_metadata_substitution(loader):
    original_dir = _write_live(loader, "binding-original", version=1, body="ORIGINAL")
    other_dir = _write_live(loader, "binding-other", version=1, body="OTHER")
    original_body = (original_dir / "SKILL.md").read_text(encoding="utf-8")
    other_body = (other_dir / "SKILL.md").read_text(encoding="utf-8")
    token = "f" * 32
    binding: list[str] = []
    _stage_update(
        loader,
        "binding-update",
        target="auto/binding-original",
        body="## Steps\n\nSAFE",
        notify=False,
        stage_token=token,
        unattended_binding_out=binding,
    )
    pending = loader._pending_root() / "binding-update"
    (pending / "SKILL.md").write_text("SUBSTITUTED", encoding="utf-8")
    meta_file = pending / ".meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    meta["target"] = "auto/binding-other"
    meta["base_content_hash"] = hashlib.sha256(other_body.encode("utf-8")).hexdigest()
    meta_file.write_text(json.dumps(meta), encoding="utf-8")

    assert (
        loader.auto_apply_pending_update(
            "binding-update",
            expected_stage_token=token,
            expected_candidate_binding=binding[0],
        )
        is None
    )
    assert (original_dir / "SKILL.md").read_text(encoding="utf-8") == original_body
    assert (other_dir / "SKILL.md").read_text(encoding="utf-8") == other_body
    assert (loader._pending_root() / "binding-update" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "SUBSTITUTED"


def test_auto_apply_inspects_only_claimed_snapshot(loader, monkeypatch):
    _write_live(loader, "snapshot", version=1, body="OLD")
    token = "c" * 32
    binding: list[str] = []
    _stage_update(
        loader,
        "snapshot-update",
        target="auto/snapshot",
        notify=False,
        stage_token=token,
        unattended_binding_out=binding,
    )
    real_layout = loader._candidate_layout_ok
    injected = False

    def inject_at_public_path(src, name):
        nonlocal injected
        assert src.parent == loader._claims_root()
        assert not (loader._pending_root() / "snapshot-update").exists()
        replacement = loader._pending_root() / "snapshot-update"
        replacement.mkdir()
        (replacement / "SKILL.md").write_text("replacement", encoding="utf-8")
        (replacement / ".meta.json").write_text("{}", encoding="utf-8")
        scripts = replacement / "scripts"
        scripts.mkdir()
        (scripts / "late.py").write_text("print('late')\n", encoding="utf-8")
        injected = True
        return real_layout(src, name)

    monkeypatch.setattr(loader, "_candidate_layout_ok", inject_at_public_path)
    assert loader.auto_apply_pending_update(
        "snapshot-update",
        expected_stage_token=token,
        expected_candidate_binding=binding[0],
    ) == (
        "auto/snapshot",
        2,
    )
    assert injected is True
    assert (loader._pending_root() / "snapshot-update" / "scripts" / "late.py").exists()
    assert not (loader._dir / "auto" / "snapshot" / "scripts" / "late.py").exists()


def test_abandoned_claim_is_recovered_by_pending_listing(loader):
    _write_live(loader, "recover", version=1, body="OLD")
    _stage_update(loader, "recover-update", target="auto/recover")
    claimed = loader._claim_pending_update("recover-update")
    assert claimed is not None
    claim, fd, _consumed_at = claimed
    assert claim.exists()
    assert not (loader._pending_root() / "recover-update").exists()

    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)
    assert [row["slug"] for row in loader.list_pending_skills()] == ["recover-update"]
    assert not claim.exists()


def test_concurrent_same_target_promotions_serialize(loader):
    _write_live(loader, "serialized", version=1, body="ORIGINAL")
    _stage_update(
        loader,
        "serialized-a",
        target="auto/serialized",
        base_version=1,
        body="## Steps\n\nFROM-A",
    )
    _stage_update(
        loader,
        "serialized-b",
        target="auto/serialized",
        base_version=1,
        body="## Steps\n\nFROM-B",
    )
    barrier = threading.Barrier(2)

    def promote(slug):
        barrier.wait()
        return loader.approve_pending_update(slug)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(promote, ["serialized-a", "serialized-b"]))

    assert results.count("auto/serialized") == 1
    assert results.count(None) == 1
    assert loader.get_auto_skill_version("auto/serialized") == 2
    assert len(loader.list_pending_skills()) == 1


def test_auto_apply_uses_version_computed_under_promotion_lock(loader, monkeypatch):
    _write_live(loader, "authoritative", version=2, body="OLD")
    token = "d" * 32
    binding: list[str] = []
    _stage_update(
        loader,
        "authoritative-update",
        target="auto/authoritative",
        base_version=2,
        notify=False,
        stage_token=token,
        unattended_binding_out=binding,
    )
    real_get_version = loader.get_auto_skill_version
    version_reads = 0

    def changed_after_first_read(name):
        nonlocal version_reads
        version_reads += 1
        if version_reads > 1:
            return 99
        return real_get_version(name)

    monkeypatch.setattr(loader, "get_auto_skill_version", changed_after_first_read)
    seen: list[dict] = []
    skills_mod.set_update_auto_applied_hook(seen.append)
    try:
        assert loader.auto_apply_pending_update(
            "authoritative-update",
            expected_stage_token=token,
            expected_candidate_binding=binding[0],
        ) == ("auto/authoritative", 3)
    finally:
        skills_mod.set_update_auto_applied_hook(None)

    assert version_reads == 1
    assert seen[0]["new_version"] == 3
    claim_locks = loader._locks_root() / "claims"
    assert not claim_locks.exists() or not list(claim_locks.glob("*.lock"))


def test_restore_failure_releases_claim_lock_for_recovery(loader, monkeypatch):
    _write_live(loader, "restore-failure", version=1, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        "restore-failure-update",
        target="auto/restore-failure",
        stage_token="original",
        unattended_binding_out=binding,
    )
    real_restore = loader._restore_claimed_update
    restore_calls = 0

    def fail_once(claim, slug):
        nonlocal restore_calls
        restore_calls += 1
        if restore_calls == 1:
            raise OSError("injected restore failure")
        return real_restore(claim, slug)

    monkeypatch.setattr(loader, "_restore_claimed_update", fail_once)
    assert (
        loader.auto_apply_pending_update(
            "restore-failure-update",
            expected_stage_token="replacement",
            expected_candidate_binding=binding[0],
        )
        is None
    )
    assert not (loader._pending_root() / "restore-failure-update").exists()

    assert [row["slug"] for row in loader.list_pending_skills()] == ["restore-failure-update"]
    assert restore_calls == 2
    claim_locks = loader._locks_root() / "claims"
    assert not claim_locks.exists() or not list(claim_locks.glob("*.lock"))


def test_new_skill_approval_inspects_only_claimed_snapshot(loader, monkeypatch):
    loader.stage_skill_candidate(
        "approve-replacement-race",
        description="original candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nORIGINAL",
        provenance=_prov(),
    )
    pending = loader._pending_root() / "approve-replacement-race"
    live = loader._dir / "auto" / "approve-replacement-race"
    inspection_started = threading.Event()
    allow_approval = threading.Event()
    real_layout = loader._candidate_layout_ok

    def blocking_layout(src, name):
        assert src.parent == loader._claims_root()
        assert not pending.exists()
        inspection_started.set()
        assert allow_approval.wait(timeout=2)
        return real_layout(src, name)

    monkeypatch.setattr(loader, "_candidate_layout_ok", blocking_layout)
    with ThreadPoolExecutor(max_workers=1) as pool:
        approval = pool.submit(loader.approve_pending_skill, "approve-replacement-race")
        assert inspection_started.wait(timeout=2)
        try:
            # Dismissal cannot touch the in-flight private claim.
            assert loader.dismiss_pending_skill("approve-replacement-race") is False

            # A direct writer can reoccupy the public slug without taking the
            # namespace lock. Approval must never inspect or consume these bytes.
            pending.mkdir()
            (pending / "SKILL.md").write_text("REPLACEMENT", encoding="utf-8")
            (pending / ".meta.json").write_text("{}", encoding="utf-8")
            scripts = pending / "scripts"
            scripts.mkdir()
            (scripts / "late.py").write_text("print('late')\n", encoding="utf-8")
        finally:
            allow_approval.set()
        assert approval.result(timeout=2) == "auto/approve-replacement-race"

    assert "ORIGINAL" in (live / "SKILL.md").read_text(encoding="utf-8")
    assert "REPLACEMENT" not in (live / "SKILL.md").read_text(encoding="utf-8")
    assert not (live / "scripts" / "late.py").exists()
    assert (pending / "scripts" / "late.py").exists()
    claim_locks = loader._locks_root() / "claims"
    assert not claim_locks.exists() or not list(claim_locks.glob("*.lock"))


def test_claim_fails_closed_outside_agent_denied_root(tmp_path):
    unprotected = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    unprotected.stage_skill_candidate(
        "unprotected-candidate",
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nORIGINAL",
        provenance=_prov(),
    )

    assert unprotected.approve_pending_skill("unprotected-candidate") is None
    assert (unprotected._pending_root() / "unprotected-candidate" / "SKILL.md").exists()
    assert not list(unprotected._claims_root().iterdir())


def test_skill_lock_files_are_prepared_for_windows(loader, monkeypatch):
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat, "try_acquire_lock", lambda _fd, exclusive=False: True
    )
    monkeypatch.setattr(skills_mod.platform_compat, "release_lock", lambda _fd: None)
    loader.stage_skill_candidate(
        "windows-lock-byte",
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nORIGINAL",
        provenance=_prov(),
    )
    pending_lock = loader._locks_root() / "pending.lock"
    assert pending_lock.read_bytes() == b"\0"

    claimed = loader._claim_pending_update("windows-lock-byte")
    assert claimed is not None
    claim, fd, _consumed_at = claimed
    claim_lock = loader._claim_lock_path(claim.name)
    assert claim_lock.read_bytes() == loader._claim_lock_state_payload(claim.name, completed=False)
    os.close(fd)
    loader._restore_claimed_update(claim, "windows-lock-byte")
    loader._cleanup_claim_lock(claim.name)


def test_skill_lock_refuses_hardlink_without_touching_target(loader):
    assert loader._private_state_roots_safe(create=True) is True
    victim = loader._locks_root() / "hardlink-victim"
    victim.write_bytes(b"DO NOT TOUCH")
    lock_path = loader._locks_root() / "hardlinked.lock"
    os.link(victim, lock_path)

    with pytest.raises(OSError):
        loader._open_skill_lock(lock_path)

    assert victim.read_bytes() == b"DO NOT TOUCH"
    assert lock_path.read_bytes() == b"DO NOT TOUCH"


def test_skill_lock_detects_opened_inode_swap_without_nofollow(loader, monkeypatch):
    assert loader._private_state_roots_safe(create=True) is True
    lock_path = loader._locks_root() / "swapped.lock"
    lock_path.write_bytes(b"")
    victim = loader._locks_root() / "swap-victim"
    victim.write_bytes(b"DO NOT TOUCH")
    real_open = skills_mod.os.open

    def open_swapped_inode(path, flags, *args, **kwargs):
        if Path(path) == lock_path:
            return real_open(victim, flags, *args, **kwargs)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.delattr(skills_mod.os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(skills_mod.os, "open", open_swapped_inode)

    with pytest.raises(OSError):
        loader._open_skill_lock(lock_path)

    assert lock_path.read_bytes() == b""
    assert victim.read_bytes() == b"DO NOT TOUCH"


def _linked_pending_candidate(loader, slug):
    victim = loader._pending_root() / f"{slug}-victim"
    victim.mkdir(parents=True, exist_ok=True)
    (victim / "SKILL.md").write_text("VICTIM", encoding="utf-8")
    metadata = '{"name": "victim", "notify_suppressed": true}'
    (victim / ".meta.json").write_text(metadata, encoding="utf-8")
    link = loader._pending_root() / slug
    skills_mod.platform_compat.symlink_or_junction(victim, link)
    return link, victim, metadata


def test_linked_claim_restores_without_touching_target_metadata(loader):
    link, victim, metadata = _linked_pending_candidate(loader, "linked-approval")

    assert loader.approve_pending_skill("linked-approval") is None

    assert skills_mod.platform_compat.is_link_or_junction(link)
    assert (victim / ".meta.json").read_text(encoding="utf-8") == metadata
    assert not (victim / ".promoted").exists()


def test_linked_claim_dismissal_unlinks_only_claim(loader):
    link, victim, metadata = _linked_pending_candidate(loader, "linked-dismissal")

    assert loader.dismiss_pending_skill("linked-dismissal") is True

    assert not skills_mod.platform_compat.is_link_or_junction(link)
    assert (victim / "SKILL.md").read_text(encoding="utf-8") == "VICTIM"
    assert (victim / ".meta.json").read_text(encoding="utf-8") == metadata
    assert not (victim / ".promoted").exists()


@pytest.mark.parametrize(
    ("method_name", "expected", "link_remains"),
    [
        ("approve_pending_skill", None, True),
        ("dismiss_pending_skill", True, False),
    ],
)
def test_completion_marker_check_does_not_follow_linked_claim_parent(
    loader, method_name, expected, link_remains
):
    slug = f"linked-marker-parent-{method_name}"
    link, victim, metadata = _linked_pending_candidate(loader, slug)
    victim_marker = victim / ".promoted"
    victim_marker.write_text("VICTIM MARKER\n", encoding="utf-8")

    assert getattr(loader, method_name)(slug) == expected

    assert skills_mod.platform_compat.is_link_or_junction(link) is link_remains
    assert (victim / "SKILL.md").read_text(encoding="utf-8") == "VICTIM"
    assert (victim / ".meta.json").read_text(encoding="utf-8") == metadata
    assert victim_marker.read_text(encoding="utf-8") == "VICTIM MARKER\n"


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_dismiss_refuses_preplanted_completion_marker_without_touching_target(
    loader, tmp_path, link_kind
):
    slug = f"marker-{link_kind}"
    loader.stage_skill_candidate(
        slug,
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nORIGINAL",
        provenance=_prov(),
    )
    marker = loader._pending_root() / slug / ".promoted"
    victim = tmp_path / f"{link_kind}-victim"
    victim.write_text("DO NOT TOUCH", encoding="utf-8")
    if link_kind == "symlink":
        os.symlink(victim, marker)
    else:
        os.link(victim, marker)

    assert loader.dismiss_pending_skill(slug) is False

    assert victim.read_text(encoding="utf-8") == "DO NOT TOUCH"
    restored = loader._pending_root() / slug
    assert restored.is_dir()
    assert not os.path.lexists(restored / ".promoted")


def test_non_object_metadata_is_restored_without_rewrite(loader):
    _stage_update(loader, "list-metadata", target="auto/target")
    meta_file = loader._pending_root() / "list-metadata" / ".meta.json"
    meta_file.write_text("[]", encoding="utf-8")

    assert loader.approve_pending_update("list-metadata") is None

    assert meta_file.read_text(encoding="utf-8") == "[]"
    assert not loader._claims_root().exists() or not list(loader._claims_root().iterdir())


def test_auto_apply_claim_failure_emits_suppressed_staged_notification(loader, monkeypatch):
    _write_live(loader, "claim-notify", version=1, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        "claim-notify-update",
        target="auto/claim-notify",
        notify=False,
        stage_token="expected",
        unattended_binding_out=binding,
    )
    seen = []
    monkeypatch.setattr(loader, "_claim_pending_update", lambda _slug: None)
    monkeypatch.setattr(loader, "emit_pending_staged", seen.append)

    assert (
        loader.auto_apply_pending_update(
            "claim-notify-update",
            expected_stage_token="expected",
            expected_candidate_binding=binding[0],
        )
        is None
    )
    assert seen == ["claim-notify-update"]


def test_concurrent_new_skill_approvals_serialize_and_preserve_replacement(loader, monkeypatch):
    slug = "concurrent-new-approval"
    loader.stage_skill_candidate(
        slug,
        description="original candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nORIGINAL",
        provenance=_prov(),
    )
    pending = loader._pending_root() / slug
    live = loader._dir / "auto" / slug
    first_inspection = threading.Event()
    allow_first = threading.Event()
    second_claimed = threading.Event()
    unexpected_second_inspection = threading.Event()
    real_claim = loader._claim_pending_update
    real_layout = loader._candidate_layout_ok
    count_lock = threading.Lock()
    claim_count = 0
    layout_count = 0

    def recording_claim(candidate_slug):
        nonlocal claim_count
        result = real_claim(candidate_slug)
        if result is not None:
            with count_lock:
                claim_count += 1
                if claim_count == 2:
                    second_claimed.set()
        return result

    def blocking_first_layout(src, name):
        nonlocal layout_count
        with count_lock:
            layout_count += 1
            current = layout_count
        if current == 1:
            first_inspection.set()
            assert allow_first.wait(timeout=2)
        else:
            unexpected_second_inspection.set()
        return real_layout(src, name)

    monkeypatch.setattr(loader, "_claim_pending_update", recording_claim)
    monkeypatch.setattr(loader, "_candidate_layout_ok", blocking_first_layout)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(loader.approve_pending_skill, slug)
        assert first_inspection.wait(timeout=2)
        pending.mkdir()
        (pending / "SKILL.md").write_text("REPLACEMENT", encoding="utf-8")
        (pending / ".meta.json").write_text("{}", encoding="utf-8")
        scripts = pending / "scripts"
        scripts.mkdir()
        (scripts / "late.py").write_text("print('late')\n", encoding="utf-8")
        second = pool.submit(loader.approve_pending_skill, slug)
        assert second_claimed.wait(timeout=2)
        assert not second.done()
        assert not unexpected_second_inspection.is_set()
        allow_first.set()
        assert first.result(timeout=2) == f"auto/{slug}"
        assert second.result(timeout=2) is None

    assert "ORIGINAL" in (live / "SKILL.md").read_text(encoding="utf-8")
    assert not any(child.name.startswith(f"{slug}--") for child in live.iterdir())
    assert (pending / "scripts" / "late.py").exists()
    assert not loader._claims_root().exists() or not list(loader._claims_root().iterdir())


def test_restore_no_replace_preserves_direct_writer(loader, monkeypatch):
    slug = "restore-direct-writer"
    _stage_update(loader, slug, target="auto/restore-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at = claimed
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)
    pending = loader._pending_root() / slug
    entered_restore = threading.Event()
    allow_restore = threading.Event()
    real_rename = skills_mod.platform_compat.rename_no_replace
    blocked = False

    def blocking_rename(src, destination):
        nonlocal blocked
        if Path(src) == claim and Path(destination) == pending and not blocked:
            blocked = True
            entered_restore.set()
            assert allow_restore.wait(timeout=2)
        return real_rename(src, destination)

    monkeypatch.setattr(skills_mod.platform_compat, "rename_no_replace", blocking_rename)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(loader._restore_claimed_update, claim, slug)
        assert entered_restore.wait(timeout=2)
        pending.mkdir()
        (pending / "SKILL.md").write_text("DIRECT REPLACEMENT", encoding="utf-8")
        allow_restore.set()
        restored = future.result(timeout=2)

    assert restored is not None and restored.name == f"{slug}-2"
    assert (pending / "SKILL.md").read_text(encoding="utf-8") == "DIRECT REPLACEMENT"
    assert "new steps" in (restored / "SKILL.md").read_text(encoding="utf-8")
    loader._cleanup_claim_lock(claim.name)


def test_claim_refuses_before_public_move_when_no_replace_is_unsupported(loader, monkeypatch):
    import errno

    slug = "unsupported-no-replace"
    _stage_update(loader, slug, target="auto/no-replace-target")
    pending = loader._pending_root() / slug

    def unsupported(_source, _destination):
        raise OSError(errno.ENOTSUP, "atomic no-replace rename unavailable")

    monkeypatch.setattr(skills_mod.platform_compat, "rename_no_replace", unsupported)

    assert loader._claim_pending_update(slug) is None
    assert pending.is_dir()
    assert "new steps" in (pending / "SKILL.md").read_text(encoding="utf-8")
    assert not list(loader._claims_root().glob(f"{slug}--*"))
    assert not list((loader._locks_root() / "claims").glob(f"{slug}--*.lock"))


def test_partial_cleanup_marker_repair_failure_uses_completed_lock_state(loader, monkeypatch):
    _write_live(loader, "lock-outcome-recovery", version=1, body="OLD")
    slug = "lock-outcome-recovery-update"
    _stage_update(loader, slug, target="auto/lock-outcome-recovery")
    real_rmtree = skills_mod.shutil.rmtree
    real_write_marker = loader._write_completion_marker
    cleanup_failed = False
    marker_writes = 0

    def remove_marker_then_fail(path, *args, **kwargs):
        nonlocal cleanup_failed
        candidate = Path(path)
        if candidate.parent == loader._claims_root() and not cleanup_failed:
            cleanup_failed = True
            assert loader._authenticated_completion_marker(candidate) is True
            (candidate / ".promoted").unlink()
            raise PermissionError("injected partial cleanup after marker removal")
        return real_rmtree(path, *args, **kwargs)

    def write_initial_marker_only(candidate):
        nonlocal marker_writes
        marker_writes += 1
        if marker_writes == 1:
            return real_write_marker(candidate)
        return False

    monkeypatch.setattr(skills_mod.shutil, "rmtree", remove_marker_then_fail)
    monkeypatch.setattr(loader, "_write_completion_marker", write_initial_marker_only)

    assert loader.approve_pending_update(slug) == "auto/lock-outcome-recovery"
    leftovers = list(loader._claims_root().glob(f"{slug}--*"))
    assert len(leftovers) == 1
    claim = leftovers[0]
    assert loader._authenticated_completion_marker(claim) is False

    lock_path = loader._claim_lock_path(claim.name)
    fd = loader._open_skill_lock(lock_path)
    acquired = skills_mod.platform_compat.try_acquire_lock(fd, exclusive=True)
    try:
        assert acquired is True
        assert loader._authenticated_claim_lock_state(fd, lock_path, claim.name, completed=True)
    finally:
        if acquired:
            skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    assert loader.list_pending_skills() == []
    assert not claim.exists()
    assert not (loader._pending_root() / slug).exists()
    assert loader.get_auto_skill_version("auto/lock-outcome-recovery") == 2


def test_partial_promoted_cleanup_recreates_authenticated_marker(loader, monkeypatch):
    _write_live(loader, "promote-partial-cleanup", version=1, body="OLD")
    _stage_update(
        loader,
        "promote-partial-cleanup-update",
        target="auto/promote-partial-cleanup",
    )
    real_rmtree = skills_mod.shutil.rmtree
    failed = False

    def partially_remove_first_claim(path, *args, **kwargs):
        nonlocal failed
        candidate = Path(path)
        if candidate.parent == loader._claims_root() and not failed:
            failed = True
            assert loader._authenticated_completion_marker(candidate) is True
            (candidate / ".promoted").unlink()
            raise PermissionError("injected Windows-style partial cleanup")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(skills_mod.shutil, "rmtree", partially_remove_first_claim)

    assert loader.approve_pending_update("promote-partial-cleanup-update") == (
        "auto/promote-partial-cleanup"
    )
    leftovers = list(loader._claims_root().iterdir())
    assert len(leftovers) == 1
    assert loader._authenticated_completion_marker(leftovers[0]) is True
    assert loader.get_auto_skill_version("auto/promote-partial-cleanup") == 2
    assert loader.list_pending_skills() == []
    assert not loader._claims_root().exists() or not list(loader._claims_root().iterdir())


def test_partial_recovery_cleanup_recreates_authenticated_marker(loader, monkeypatch):
    _stage_update(loader, "recovery-partial-cleanup", target="auto/recovery-target")
    claimed = loader._claim_pending_update("recovery-partial-cleanup")
    assert claimed is not None
    claim, fd, _consumed_at = claimed
    assert loader._write_completion_marker(claim) is True
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)
    real_rmtree = skills_mod.shutil.rmtree
    failed = False

    def partially_remove_first_claim(path, *args, **kwargs):
        nonlocal failed
        candidate = Path(path)
        if candidate == claim and not failed:
            failed = True
            assert loader._authenticated_completion_marker(candidate) is True
            (candidate / ".promoted").unlink()
            raise PermissionError("injected Windows-style partial cleanup")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(skills_mod.shutil, "rmtree", partially_remove_first_claim)

    assert loader.list_pending_skills() == []
    assert claim.is_dir()
    assert loader._authenticated_completion_marker(claim) is True
    assert loader.list_pending_skills() == []
    assert not claim.exists()


def test_failed_dismiss_cleanup_is_recovered(loader, monkeypatch):
    slug = "dismiss-cleanup-failure"
    loader.stage_skill_candidate(
        slug,
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nORIGINAL",
        provenance=_prov(),
    )
    real_rmtree = skills_mod.shutil.rmtree
    failed = False

    def fail_first_claim_cleanup(path, *args, **kwargs):
        nonlocal failed
        candidate = Path(path)
        if candidate.parent == loader._claims_root() and not failed:
            failed = True
            assert loader._authenticated_completion_marker(candidate) is True
            (candidate / ".promoted").unlink()
            raise PermissionError("injected Windows-style partial cleanup")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(skills_mod.shutil, "rmtree", fail_first_claim_cleanup)

    assert loader.dismiss_pending_skill(slug) is True
    leftovers = list(loader._claims_root().iterdir())
    assert len(leftovers) == 1
    assert loader._authenticated_completion_marker(leftovers[0]) is True
    assert loader.list_pending_skills() == []
    assert not loader._claims_root().exists() or not list(loader._claims_root().iterdir())


def test_pending_slug_claim_requires_exact_double_hyphen_prefix(loader):
    claim = loader._claims_root() / "foo--x--0123456789abcdef"
    claim.mkdir(parents=True)

    assert loader._pending_slug_claimed("foo--x") is True
    assert loader._pending_slug_claimed("foo") is False
    assert (
        loader.stage_skill_candidate(
            "foo",
            description="distinct candidate",
            triggers="foo",
            procedure_md="body",
            provenance=_prov(),
        )
        == "auto/foo"
    )


def test_dismiss_pending_serializes_with_atomic_claim(loader, monkeypatch):
    _write_live(loader, "dismiss-race", version=1, body="OLD")
    _stage_update(loader, "dismiss-race-update", target="auto/dismiss-race")
    pending = loader._pending_root() / "dismiss-race-update"
    entered_rename = threading.Event()
    allow_rename = threading.Event()
    claim_started = threading.Event()
    real_rename = skills_mod.os.rename

    def blocking_rename(src, dest, *args, **kwargs):
        if os.fspath(src) == os.fspath(pending):
            entered_rename.set()
            assert allow_rename.wait(timeout=2)
        return real_rename(src, dest, *args, **kwargs)

    def claim():
        claim_started.set()
        return loader._claim_pending_update("dismiss-race-update")

    monkeypatch.setattr(skills_mod.os, "rename", blocking_rename)
    with ThreadPoolExecutor(max_workers=2) as pool:
        dismissed = pool.submit(loader.dismiss_pending_skill, "dismiss-race-update")
        assert entered_rename.wait(timeout=2)
        claimed = pool.submit(claim)
        assert claim_started.wait(timeout=2)
        assert not claimed.done()
        allow_rename.set()
        assert dismissed.result(timeout=2) is True
        assert claimed.result(timeout=2) is None

    assert not pending.exists()
    assert not loader._claims_root().exists() or not list(loader._claims_root().iterdir())


def test_failed_claim_attempt_removes_unique_lock_file(loader):
    assert loader._claim_pending_update("missing-update") is None

    claim_locks = loader._locks_root() / "claims"
    assert not claim_locks.exists() or not list(claim_locks.glob("*.lock"))


def test_restore_claimed_update_does_not_follow_symlinked_meta(loader, monkeypatch):
    _write_live(loader, "symlinked-meta", version=1, body="OLD")
    _stage_update(loader, "symlinked-meta-update", target="auto/symlinked-meta")
    claimed = loader._claim_pending_update("symlinked-meta-update")
    assert claimed is not None
    claim, fd, _consumed_at = claimed
    meta_file = claim / ".meta.json"
    path_type = type(meta_file)
    real_is_link_or_junction = skills_mod.is_link_or_junction
    real_read_text = path_type.read_text

    def fake_is_link_or_junction(path):
        return Path(path) == meta_file or real_is_link_or_junction(path)

    def guarded_read_text(path, *args, **kwargs):
        if path == meta_file:
            raise AssertionError("restore followed symlinked candidate metadata")
        return real_read_text(path, *args, **kwargs)

    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)
    monkeypatch.setattr(skills_mod, "is_link_or_junction", fake_is_link_or_junction)
    monkeypatch.setattr(path_type, "read_text", guarded_read_text)

    loader._restore_claimed_update(claim, "symlinked-meta-update")

    assert (loader._pending_root() / "symlinked-meta-update").is_dir()
    loader._cleanup_claim_lock(claim.name)


def test_spoofed_abandoned_completion_marker_restores_claim(loader):
    _write_live(loader, "spoofed-recovery", version=1, body="OLD")
    _stage_update(
        loader,
        "spoofed-recovery-update",
        target="auto/spoofed-recovery",
    )
    claimed = loader._claim_pending_update("spoofed-recovery-update")
    assert claimed is not None
    claim, fd, _consumed_at = claimed
    (claim / ".promoted").write_text("not-this-claim\n", encoding="utf-8")
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    assert [row["slug"] for row in loader.list_pending_skills()] == ["spoofed-recovery-update"]
    restored = loader._pending_root() / "spoofed-recovery-update"
    assert restored.is_dir()
    assert not (restored / ".promoted").exists()
    assert not claim.exists()


def test_promoted_abandoned_claim_is_deleted_instead_of_restored(loader):
    _write_live(loader, "promoted-recovery", version=1, body="OLD")
    _stage_update(
        loader,
        "promoted-recovery-update",
        target="auto/promoted-recovery",
    )
    claimed = loader._claim_pending_update("promoted-recovery-update")
    assert claimed is not None
    claim, fd, _consumed_at = claimed
    assert loader._write_completion_marker(claim) is True
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    assert loader.list_pending_skills() == []
    assert not claim.exists()
    claim_locks = loader._locks_root() / "claims"
    assert not claim_locks.exists() or not list(claim_locks.glob("*.lock"))


# ── reserved namespace and claimed-inode hardening ──


@pytest.mark.parametrize("name", ["auto", "AUTO", "Auto", "aUtO"])
def test_delete_refuses_bare_reserved_auto_namespace(loader, monkeypatch, name):
    live_dir = _write_live(loader, "namespace-survivor", version=1, body="KEEP")

    def unexpected_delete(_name):
        pytest.fail("reserved auto namespace reached the recursive delete helper")

    monkeypatch.setattr(loader, "_delete_skill_unlocked", unexpected_delete)

    assert loader.delete_skill(name) is False
    assert (live_dir / "SKILL.md").exists()


def test_approval_refuses_candidate_mutated_through_hardlink_after_claim(loader, monkeypatch):
    slug = "hardlinked-candidate"
    assert (
        loader.stage_skill_candidate(
            slug,
            description="hardlink isolation",
            triggers="hardlink",
            procedure_md="## Steps\n\nORIGINAL",
            provenance=_prov(),
        )
        == f"auto/{slug}"
    )
    pending_skill = loader._pending_root() / slug / "SKILL.md"
    alias = loader._dir / "candidate-hardlink-alias"
    os.link(pending_skill, alias)
    assert pending_skill.stat().st_nlink == 2

    real_layout_check = loader._candidate_layout_ok

    def mutate_after_claim(src, name):
        assert src.parent == loader._claims_root()
        alias.write_text("MUTATED AFTER CLAIM\n", encoding="utf-8")
        return real_layout_check(src, name)

    monkeypatch.setattr(loader, "_candidate_layout_ok", mutate_after_claim)

    assert loader.approve_pending_skill(slug) is None
    restored = loader._pending_root() / slug / "SKILL.md"
    assert restored.read_text(encoding="utf-8") == "MUTATED AFTER CLAIM\n"
    assert not (loader._dir / "auto" / slug).exists()


def test_candidate_inode_guard_fails_closed_on_stat_error(loader, monkeypatch):
    slug = "unstatable-candidate"
    assert (
        loader.stage_skill_candidate(
            slug,
            description="stat failure",
            triggers="stat",
            procedure_md="## Steps\n\nBODY",
            provenance=_prov(),
        )
        == f"auto/{slug}"
    )
    skill_file = loader._pending_root() / slug / "SKILL.md"
    real_lstat = skills_mod.os.lstat

    def fail_candidate_stat(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(skill_file):
            raise PermissionError("injected candidate stat failure")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(skills_mod.os, "lstat", fail_candidate_stat)

    assert loader.get_pending_skill(slug) is None
