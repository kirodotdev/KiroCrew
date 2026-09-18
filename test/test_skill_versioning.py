"""Tests: pending skill UPDATES + per-skill version history.

Covers ``stage_skill_candidate(kind="update", ...)``, ``get_auto_skill_version``,
``read_auto_skill_body`` and ``approve_pending_update`` — the update-approval
flow that snapshots the current live version into ``auto/<slug>/.versions/``
before overwriting, and the guarantee that ``.versions`` never surfaces as a
loadable skill.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import secrets
import shutil
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

import kiro_crew.skills as skills_mod
from kiro_crew.skills import (
    MAX_SKILL_VERSIONS,
    AutoSkillProvenance,
    SkillsLoader,
    canonical_skill_text_hash,
)


@pytest.fixture()
def loader():
    return SkillsLoader(install_builtins=False)


@pytest.fixture(params=("native", "fallback"))
def snapshot_reader(request, monkeypatch):
    if request.param == "fallback":
        monkeypatch.setattr(
            SkillsLoader,
            "_skill_tree_snapshot",
            staticmethod(SkillsLoader._skill_tree_snapshot_by_name),
        )
    return request.param


@pytest.fixture(params=("native", "fallback"))
def private_state_mode(request, monkeypatch):
    if request.param == "fallback":
        monkeypatch.setattr(skills_mod, "_DIR_FD_SUPPORTED", False)
    return request.param


def _open_lock_for_test(loader, path):
    with loader._pin_private_state(create=False) as private_state:
        parent = (
            private_state.claim_locks
            if path.parent.name == skills_mod.AUTO_CLAIMS_DIRNAME
            else private_state.locks
        )
        return loader._open_skill_lock(parent, path.name)


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
    unattended=False,
    base_content_hash=None,
    unattended_binding_out=None,
):
    if base_content_hash is None and unattended:
        live_body = loader.read_auto_skill_body(target)
        assert live_body is not None
        base_content_hash = canonical_skill_text_hash(live_body)
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
    metadata_file = pending / ".meta.json"
    normalized_metadata = metadata_file.read_bytes().replace(b"\r\n", b"\n")
    metadata_before = normalized_metadata.replace(b"\n", b"\r\n")
    metadata_file.write_bytes(metadata_before)
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
    # Snapshot validation fails → abort before any live mutation.
    monkeypatch.setattr(loader, "_validate_and_redact_candidate", lambda *a, **k: None)
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

    real_atomic_write = skills_mod.atomic_write

    def boom(path, content, **kwargs):
        candidate = Path(path)
        if candidate.name == "go.py" and candidate.parents[1].name.startswith(".publish-"):
            raise OSError("read-only scripts dir")
        return real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(skills_mod, "atomic_write", boom)
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

    real_atomic_write = skills_mod.atomic_write

    def boom(path, content, **kwargs):
        candidate = Path(path)
        if candidate.name == "b.py" and candidate.parents[1].name.startswith(".publish-"):
            raise OSError("disk full")
        return real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(skills_mod, "atomic_write", boom)
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


def test_approve_update_never_copies_a_transient_live_credential_symlink(
    loader, tmp_path, monkeypatch
):
    """A live file swapped after capture is refused without copying its target bytes."""
    if os.name == "nt":
        pytest.skip("symlink creation needs privileges on Windows")
    _write_live(loader, "transient-live", version=1, body="OLD")
    live_dir = loader._dir / "auto" / "transient-live"
    scripts = live_dir / "scripts"
    scripts.mkdir()
    live_script = scripts / "run.py"
    live_script.write_text("print('safe')\n", encoding="utf-8")
    secret = tmp_path / "credentials"
    secret.write_text("aws_secret_access_key = SHOULD-NEVER-BE-COPIED\n", encoding="utf-8")
    _stage_update(loader, "transient-live-update", target="auto/transient-live")

    real_resolve = loader._resolve_snapshot_version
    real_copy2 = shutil.copy2

    def swap_after_snapshot(*args, **kwargs):
        version = real_resolve(*args, **kwargs)
        live_script.unlink()
        live_script.symlink_to(secret)
        return version

    def reject_credential_copy(source, destination, *args, **kwargs):
        if Path(source).is_symlink() and Path(source).resolve() == secret:
            pytest.fail("live-tree retention followed a transient credential symlink")
        return real_copy2(source, destination, *args, **kwargs)

    monkeypatch.setattr(loader, "_resolve_snapshot_version", swap_after_snapshot)
    monkeypatch.setattr(skills_mod.shutil, "copy2", reject_credential_copy)

    assert loader.approve_pending_update("transient-live-update") is None
    assert secret.read_text(encoding="utf-8") == (
        "aws_secret_access_key = SHOULD-NEVER-BE-COPIED\n"
    )
    private = loader._private_root()
    assert not list(private.glob(".publish-*"))
    assert not list(private.glob(".previous-*"))


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


def test_approve_update_stale_base_gate_survives_a_poisoned_mtime_cache(loader):
    """The frontmatter cache is keyed by mtime alone, so a cross-process writer
    replacing the live skill within one mtime tick leaves this process's cache
    asserting the OLD version. The locked approve path must not trust it: the
    entry is dropped once the target lock is held, so the staleness gate reads
    the version actually on disk and refuses the v1-based candidate."""
    _write_live(loader, "tick", version=2, body="LIVE-V2")
    live = loader._dir / "auto" / "tick" / "SKILL.md"
    # Simulate the same-tick stale read: cache claims v1 under the CURRENT mtime,
    # while the bytes on disk say v2 — exactly what a same-tick replacement
    # leaves behind. Without the under-lock invalidation, the v1-based candidate
    # below would pass the base_version gate and clobber v2.
    loader._fm_cache[str(live)] = (live.stat().st_mtime, {"version": "1"})
    _stage_update(
        loader, "tick-old", target="auto/tick", base_version=1, body="## Steps\n\nOLD-MERGE"
    )

    assert loader.approve_pending_update("tick-old") is None
    body = live.read_text(encoding="utf-8")
    assert "LIVE-V2" in body and "OLD-MERGE" not in body
    assert (loader._pending_root() / "tick-old" / "SKILL.md").exists()


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


@pytest.mark.parametrize("root_kind", ["private", "claims", "evidence", "locks"])
def test_claim_rejects_linked_private_state_roots(loader, tmp_path, root_kind):
    _write_live(loader, "linked-private-root", version=1, body="OLD")
    _stage_update(
        loader,
        f"linked-{root_kind}-root-update",
        target="auto/linked-private-root",
    )
    private_root = loader._private_root()
    claims_root = loader._claims_root()
    evidence_root = loader._evidence_root()
    locks_root = loader._locks_root()
    if root_kind == "private":
        link = private_root
        shutil.rmtree(private_root)
    elif root_kind == "claims":
        link = claims_root
        claims_root.rmdir()
    elif root_kind == "evidence":
        link = evidence_root
        evidence_root.rmdir()
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


def test_loader_init_does_no_private_root_filesystem_work(tmp_path):
    """Construction performs NO private-root filesystem work.

    The gateway constructs a loader on its boot path, before the dashboard
    socket is up, so ``__init__`` must not stat/resolve/mkdir the claim/lock
    tree. The sandbox launcher owns the spawn-time masking guarantee: the child
    pre-creates ``skills/auto/.private`` before its isdir-guarded hide loop,
    which anchors the property at the point it protects.
    """
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    fresh = SkillsLoader(skills_path=skills_root, install_builtins=False)

    assert not os.path.lexists(fresh._private_root())
    # Use-time creation still works on demand.
    assert fresh._private_state_roots_safe(create=True) is True
    assert fresh._private_root().is_dir()


def test_auto_apply_prose_update_snapshots_and_notifies(loader, snapshot_reader):
    _write_live(loader, "auto-prose", version=2, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        "auto-prose-update",
        target="auto/auto-prose",
        base_version=2,
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )
    seen: list[dict] = []
    skills_mod.set_update_auto_applied_hook(seen.append)
    try:
        assert loader.auto_apply_pending_update(
            "auto-prose-update",
            expected_candidate_binding=binding[0],
        ) == ("auto/auto-prose", 3)
    finally:
        skills_mod.set_update_auto_applied_hook(None)

    assert loader.get_auto_skill_version("auto/auto-prose") == 3
    assert (loader._dir / "auto" / "auto-prose" / ".versions" / "v2-SKILL.md").exists()
    assert not (loader._pending_root() / "auto-prose-update").exists()
    assert seen[0]["new_version"] == 3


def test_auto_apply_binding_uses_exact_staged_bytes(loader, snapshot_reader):
    """The bytes on disk after staging ARE the binding's bytes, verbatim.

    Staging encodes the validated content once and writes it with
    ``write_bytes`` — no text-mode newline translation layer exists between
    the validated content and the file, so the binding (computed from the
    same in-memory byte object) always matches an untampered candidate and
    promotion succeeds. Any divergence between disk and binding is therefore
    a real tamper and is refused (see
    test_staging_binds_validated_bytes_not_file_reread).
    """
    _write_live(loader, "windows-newlines", version=1, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        "windows-newlines-update",
        target="auto/windows-newlines",
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )
    staged = loader._pending_root() / "windows-newlines-update" / "SKILL.md"
    assert b"\r\n" not in staged.read_bytes()

    assert loader.auto_apply_pending_update(
        "windows-newlines-update",
        expected_candidate_binding=binding[0],
    ) == ("auto/windows-newlines", 2)


@pytest.mark.parametrize(
    "text",
    ["line one\nline two\n", "line one\r\nline two\r\n", "line one\rline two\r"],
)
def test_canonical_skill_text_hash_ignores_newline_spelling(text):
    expected = hashlib.sha256(b"line one\nline two\n").hexdigest()
    assert canonical_skill_text_hash(text) == expected
    assert canonical_skill_text_hash(text.encode("utf-8")) == expected


def test_auto_apply_base_hash_accepts_crlf_lf_equivalence(loader, snapshot_reader):
    live_dir = _write_live(loader, "crlf-live", version=1, body="OLD")
    live_file = live_dir / "SKILL.md"
    logical_text = live_file.read_text(encoding="utf-8")
    live_file.write_bytes(logical_text.replace("\n", "\r\n").encode("utf-8"))
    binding: list[str] = []
    _stage_update(
        loader,
        "crlf-live-update",
        target="auto/crlf-live",
        notify=False,
        unattended=True,
        base_content_hash=canonical_skill_text_hash(logical_text),
        unattended_binding_out=binding,
    )

    assert loader.auto_apply_pending_update(
        "crlf-live-update",
        expected_candidate_binding=binding[0],
    ) == ("auto/crlf-live", 2)


def test_auto_apply_base_hash_rejects_real_text_change_with_crlf(loader, snapshot_reader):
    live_dir = _write_live(loader, "crlf-drift", version=1, body="OLD")
    live_file = live_dir / "SKILL.md"
    original = live_file.read_text(encoding="utf-8")
    live_file.write_bytes(original.replace("\n", "\r\n").encode("utf-8"))
    binding: list[str] = []
    _stage_update(
        loader,
        "crlf-drift-update",
        target="auto/crlf-drift",
        notify=False,
        unattended=True,
        base_content_hash=canonical_skill_text_hash(original),
        unattended_binding_out=binding,
    )
    changed = original.replace("OLD", "CONCURRENT EDIT")
    live_file.write_bytes(changed.replace("\n", "\r\n").encode("utf-8"))

    assert (
        loader.auto_apply_pending_update(
            "crlf-drift-update",
            expected_candidate_binding=binding[0],
        )
        is None
    )
    assert "CONCURRENT EDIT" in live_file.read_text(encoding="utf-8")
    assert (loader._pending_root() / "crlf-drift-update").is_dir()


def test_auto_apply_refuses_body_mutated_after_binding_check(loader, monkeypatch):
    """A retained pre-claim handle rewriting SKILL.md after the claim but
    before the binding verification must be refused — the binding check reads
    the current bytes and finds they differ from the staged binding."""
    _write_live(loader, "handle-tamper", version=1, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        "handle-tamper-update",
        target="auto/handle-tamper",
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )
    real_layout_ok = loader._candidate_layout_ok

    def layout_ok_then_tamper(src, name):
        ok = real_layout_ok(src, name)
        if ok:
            # Lands after the claim rename but BEFORE the binding read: the
            # binding check must observe the tamper and refuse.
            (src / "SKILL.md").write_text("## Steps\n\nTAMPERED\n", encoding="utf-8")
        return ok

    monkeypatch.setattr(loader, "_candidate_layout_ok", layout_ok_then_tamper)
    assert (
        loader.auto_apply_pending_update(
            "handle-tamper-update",
            expected_candidate_binding=binding[0],
        )
        is None
    )
    # Live untouched; candidate restored for review.
    assert loader.get_auto_skill_version("auto/handle-tamper") == 1
    assert "TAMPERED" not in (loader._dir / "auto" / "handle-tamper" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert (loader._pending_root() / "handle-tamper-update" / "SKILL.md").exists()


def test_auto_apply_promotes_verified_bytes_not_post_redaction_file(
    loader, monkeypatch, snapshot_reader
):
    """A retained handle rewriting SKILL.md AFTER redaction must not reach the
    live skill: the promoted body derives from the verified bytes in memory."""
    _write_live(loader, "post-redact", version=1, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        "post-redact-update",
        target="auto/post-redact",
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )
    real_version = loader.get_auto_skill_version

    def tamper_then_version(name):
        # Lands AFTER the binding verification (the version read happens later
        # in the promotion): the file-side tamper must be irrelevant because
        # the promoted body derives from the verified bytes in memory.
        for claim in loader._claims_root().glob("post-redact-update--*"):
            (claim / "SKILL.md").write_text("## Steps\n\nLATE-TAMPER\n", encoding="utf-8")
        return real_version(name)

    monkeypatch.setattr(loader, "get_auto_skill_version", tamper_then_version)
    assert loader.auto_apply_pending_update(
        "post-redact-update",
        expected_candidate_binding=binding[0],
    ) == ("auto/post-redact", 2)
    live_body = (loader._dir / "auto" / "post-redact" / "SKILL.md").read_text(encoding="utf-8")
    assert "LATE-TAMPER" not in live_body


def test_auto_apply_never_copies_scripts_planted_after_probe(loader, monkeypatch):
    """A scripts dir created after the refuse_scripts probe (via a retained
    directory handle) must never be copied into the live tree."""
    _write_live(loader, "late-scripts", version=1, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        "late-scripts-update",
        target="auto/late-scripts",
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )
    real_version = loader.get_auto_skill_version

    def plant_scripts_then_version(name):
        # Lands AFTER the refuse_scripts probe (the version read happens later
        # in the promotion): a scripts dir planted via a retained directory
        # handle must never be copied into the live tree.
        for claim in loader._claims_root().glob("late-scripts-update--*"):
            sdir = claim / "scripts"
            sdir.mkdir(exist_ok=True)
            (sdir / "evil.py").write_text("print('planted')\n", encoding="utf-8")
        return real_version(name)

    monkeypatch.setattr(loader, "get_auto_skill_version", plant_scripts_then_version)
    result = loader.auto_apply_pending_update(
        "late-scripts-update",
        expected_candidate_binding=binding[0],
    )
    live_scripts = loader._dir / "auto" / "late-scripts" / "scripts"
    assert not (live_scripts / "evil.py").exists()
    # Whether the promotion succeeded or refused, the planted script must
    # never have reached the live tree.
    if result is not None:
        assert result == ("auto/late-scripts", 2)


def test_auto_apply_refuses_concurrent_dashboard_edit(loader, monkeypatch):
    live_dir = _write_live(loader, "dashboard-race", version=1, body="OLD")
    live_file = live_dir / "SKILL.md"
    binding: list[str] = []
    _stage_update(
        loader,
        "dashboard-race-update",
        target="auto/dashboard-race",
        base_version=1,
        notify=False,
        unattended=True,
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
    binding: list[str] = []
    _stage_update(
        loader,
        "physical-update",
        target="auto/physical",
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )
    script_dir = loader._pending_root() / "physical-update" / "scripts"
    script_dir.mkdir()
    (script_dir / "late.py").write_text("print('late')\n", encoding="utf-8")

    assert (
        loader.auto_apply_pending_update(
            "physical-update",
            expected_candidate_binding=binding[0],
        )
        is None
    )
    assert (loader._pending_root() / "physical-update" / "scripts" / "late.py").exists()
    assert loader.get_auto_skill_version("auto/physical") == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_auto_apply_refuses_a_scripts_symlink_without_following_it(loader, tmp_path):
    """The refuse-scripts gate runs BEFORE _candidate_layout_ok's no-follow
    validation, so it must probe the ``scripts`` entry without traversal — a
    followed link's target (a UNC path on Windows) is dereferenced before any
    guard sees it. A dangling link makes the no-follow property observable:
    ``exists()`` returns False for it (the gate would pass), ``lexists`` sees
    the entry and refuses."""
    _write_live(loader, "linked", version=1, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        "linked-update",
        target="auto/linked",
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )
    # Dangling symlink: never resolvable, so any traversal-based check misses it.
    (loader._pending_root() / "linked-update" / "scripts").symlink_to(tmp_path / "absent")

    assert (
        loader.auto_apply_pending_update(
            "linked-update",
            expected_candidate_binding=binding[0],
        )
        is None
    )
    assert loader.get_auto_skill_version("auto/linked") == 1
    assert "OLD" in (loader._dir / "auto" / "linked" / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_claimed_tree_is_layout_validated_before_the_binding_read(loader, tmp_path, monkeypatch):
    """INVARIANT: _approve_claimed_update_locked validates the claimed tree
    before ANY child-path dereference. A planted SKILL.md symlink must be
    rejected by the layout guard without the binding read ever opening it —
    ``read_bytes`` on the link would traverse its target first."""
    _write_live(loader, "deref", version=1, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        "deref-update",
        target="auto/deref",
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )
    cand = loader._pending_root() / "deref-update"
    lure = tmp_path / "lure.md"
    lure.write_text("LURE", encoding="utf-8")
    (cand / "SKILL.md").unlink()
    (cand / "SKILL.md").symlink_to(lure)

    opened: list[str] = []
    real_read_bytes = Path.read_bytes

    def _spy_read_bytes(self):
        if self.name == "SKILL.md" and "deref" in str(self):
            opened.append(str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _spy_read_bytes)
    assert (
        loader.auto_apply_pending_update(
            "deref-update",
            expected_candidate_binding=binding[0],
        )
        is None
    )
    assert opened == []  # the linked SKILL.md was never opened
    assert loader.get_auto_skill_version("auto/deref") == 1


@pytest.mark.parametrize("alias", ["Foo", "foo.", "foo ", "fo", "x" * 65])
def test_promotion_lock_refuses_non_canonical_slugs(loader, alias):
    """The lock file is NAMED by the slug while the live directory is RESOLVED
    by the filesystem: on a case-insensitive filesystem ``Foo`` opens
    ``auto/foo`` but locks ``target-Foo.lock`` (Win32 also strips trailing
    dots/spaces), so two writers hold different locks over one directory. The
    choke point must refuse any slug the creation paths could not have made."""
    with loader._promotion_lock(alias) as acquired:
        assert acquired is False
    # The canonical form still locks normally.
    with loader._promotion_lock("foo") as acquired:
        assert acquired is True


def test_auto_apply_refuses_candidate_and_metadata_substitution(loader):
    original_dir = _write_live(loader, "binding-original", version=1, body="ORIGINAL")
    other_dir = _write_live(loader, "binding-other", version=1, body="OTHER")
    original_body = (original_dir / "SKILL.md").read_text(encoding="utf-8")
    other_body = (other_dir / "SKILL.md").read_text(encoding="utf-8")
    binding: list[str] = []
    _stage_update(
        loader,
        "binding-update",
        target="auto/binding-original",
        body="## Steps\n\nSAFE",
        notify=False,
        unattended=True,
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
            expected_candidate_binding=binding[0],
        )
        is None
    )
    assert (original_dir / "SKILL.md").read_text(encoding="utf-8") == original_body
    assert (other_dir / "SKILL.md").read_text(encoding="utf-8") == other_body
    assert (loader._pending_root() / "binding-update" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "SUBSTITUTED"


def test_auto_apply_inspects_only_claimed_snapshot(loader, monkeypatch, snapshot_reader):
    _write_live(loader, "snapshot", version=1, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        "snapshot-update",
        target="auto/snapshot",
        notify=False,
        unattended=True,
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
        expected_candidate_binding=binding[0],
    ) == (
        "auto/snapshot",
        2,
    )
    assert injected is True
    assert (loader._pending_root() / "snapshot-update" / "scripts" / "late.py").exists()
    assert not (loader._dir / "auto" / "snapshot" / "scripts" / "late.py").exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows non-delete-sharing file handles block the claim rename",
)
def test_claim_snapshot_boundary_distinguishes_preexisting_and_retained_writes(loader, monkeypatch):
    """Only the generation captured before rename may become live.

    A write completed before the user approves is the current pending candidate
    and remains reviewable. A retained POSIX descriptor writing during the
    rename-to-claim interval is a mutation after the authoritative snapshot and
    must leave the live target unchanged.
    """
    first = _write_live(loader, "claim-boundary", version=1, body="OLD")
    slug = "claim-boundary-update"
    _stage_update(
        loader,
        slug,
        target="auto/claim-boundary",
        body="## Steps\n\nBEFORE-CLAIM",
    )
    pending_body = loader._pending_root() / slug / "SKILL.md"
    original = pending_body.read_text(encoding="utf-8")
    pending_body.write_text(
        original.replace("BEFORE-CLAIM", "CURRENT-BEFORE-CLAIM"),
        encoding="utf-8",
    )
    assert loader.approve_pending_update(slug) == "auto/claim-boundary"
    assert "CURRENT-BEFORE-CLAIM" in (first / "SKILL.md").read_text(encoding="utf-8")

    _write_live(loader, "claim-boundary-after", version=1, body="OLD")
    after_slug = "claim-boundary-after-update"
    _stage_update(
        loader,
        after_slug,
        target="auto/claim-boundary-after",
        body="## Steps\n\nSAFE-BEFORE-RENAME",
    )
    retained_fd = os.open(loader._pending_root() / after_slug / "SKILL.md", os.O_WRONLY)
    real_rename = loader._rename_skill_child_no_replace
    mutated = False

    def rename_then_write(source, source_name, destination, destination_name):
        nonlocal mutated
        result = real_rename(source, source_name, destination, destination_name)
        if source_name == after_slug:
            os.lseek(retained_fd, 0, os.SEEK_SET)
            os.ftruncate(retained_fd, 0)
            os.write(retained_fd, b"## Steps\n\nRETAINED-WRITE\n")
            mutated = True
        return result

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", rename_then_write)
    try:
        assert loader.approve_pending_update(after_slug) is None
    finally:
        os.close(retained_fd)

    assert mutated is True
    assert "OLD" in (loader._dir / "auto" / "claim-boundary-after" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert [row["slug"] for row in loader.list_pending_skills()] == [after_slug]


@pytest.mark.skipif(
    os.open not in os.supports_dir_fd or os.unlink not in os.supports_dir_fd,
    reason="retained directory descriptors require POSIX openat/unlinkat",
)
def test_claim_rejects_metadata_recreated_through_retained_directory_fd(loader, monkeypatch):
    """A pre-claim directory handle cannot retarget the approved update."""
    first = _write_live(loader, "retained-first", version=1, body="FIRST")
    second = _write_live(loader, "retained-second", version=1, body="SECOND")
    slug = "retained-metadata-update"
    _stage_update(loader, slug, target="auto/retained-first", body="## Steps\n\nSAFE")
    pending = loader._pending_root() / slug
    retained_fd = os.open(pending, os.O_RDONLY | os.O_DIRECTORY)
    real_claim = loader._claim_pending_update

    def claim_then_retarget(requested_slug):
        claimed = real_claim(requested_slug)
        assert claimed is not None
        os.unlink(".meta.json", dir_fd=retained_fd)
        replacement = {
            "slug": slug,
            "name": f"auto/{slug}",
            "kind": "update",
            "target": "auto/retained-second",
            "base_version": 1,
        }
        fd = os.open(
            ".meta.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=retained_fd,
        )
        try:
            os.write(fd, json.dumps(replacement).encode("utf-8"))
        finally:
            os.close(fd)
        return claimed

    monkeypatch.setattr(loader, "_claim_pending_update", claim_then_retarget)
    try:
        assert loader.approve_pending_update(slug) is None
    finally:
        os.close(retained_fd)

    assert "FIRST" in (first / "SKILL.md").read_text(encoding="utf-8")
    assert "SECOND" in (second / "SKILL.md").read_text(encoding="utf-8")
    assert (loader._pending_root() / slug / "SKILL.md").is_file()


def test_claim_rejects_windows_style_metadata_replacement_after_rename(loader, monkeypatch):
    """The by-name platform branch enforces the same complete-generation digest."""
    first = _write_live(loader, "path-first", version=1, body="FIRST")
    second = _write_live(loader, "path-second", version=1, body="SECOND")
    slug = "path-metadata-update"
    _stage_update(loader, slug, target="auto/path-first", body="## Steps\n\nSAFE")
    real_claim = loader._claim_pending_update

    def claim_then_retarget(requested_slug):
        claimed = real_claim(requested_slug)
        assert claimed is not None
        claim = claimed[0]
        metadata = claim / ".meta.json"
        metadata.unlink()
        metadata.write_text(
            json.dumps(
                {
                    "slug": slug,
                    "name": f"auto/{slug}",
                    "kind": "update",
                    "target": "auto/path-second",
                    "base_version": 1,
                }
            ),
            encoding="utf-8",
        )
        return claimed

    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    monkeypatch.setattr(loader, "_claim_pending_update", claim_then_retarget)

    assert loader.approve_pending_update(slug) is None
    assert "FIRST" in (first / "SKILL.md").read_text(encoding="utf-8")
    assert "SECOND" in (second / "SKILL.md").read_text(encoding="utf-8")
    assert (loader._pending_root() / slug / "SKILL.md").is_file()


def test_abandoned_claim_is_recovered_by_pending_listing(loader):
    _write_live(loader, "recover", version=1, body="OLD")
    _stage_update(loader, "recover-update", target="auto/recover")
    claimed = loader._claim_pending_update("recover-update")
    assert claimed is not None
    claim, fd, _consumed_at, _generation = claimed
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


def test_auto_apply_uses_version_from_locked_live_snapshot(loader, monkeypatch, snapshot_reader):
    _write_live(loader, "authoritative", version=2, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        "authoritative-update",
        target="auto/authoritative",
        base_version=2,
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )

    def unexpected_version_reread(_name):
        pytest.fail("promotion re-read version outside the immutable live snapshot")

    monkeypatch.setattr(loader, "get_auto_skill_version", unexpected_version_reread)
    seen: list[dict] = []
    skills_mod.set_update_auto_applied_hook(seen.append)
    try:
        assert loader.auto_apply_pending_update(
            "authoritative-update",
            expected_candidate_binding=binding[0],
        ) == ("auto/authoritative", 3)
    finally:
        skills_mod.set_update_auto_applied_hook(None)

    assert seen[0]["new_version"] == 3
    claim_locks = loader._locks_root() / "claims"
    assert not claim_locks.exists() or not list(claim_locks.glob("*.lock"))


def test_restore_failure_releases_claim_lock_for_recovery(loader, monkeypatch):
    _write_live(loader, "restore-failure", version=1, body="OLD")
    _stage_update(
        loader,
        "restore-failure-update",
        target="auto/restore-failure",
        unattended=True,
    )
    real_restore = loader._restore_claimed_update
    restore_calls = 0

    def fail_once(claim, claim_fd, slug, claim_snapshot=None):
        nonlocal restore_calls
        restore_calls += 1
        if restore_calls == 1:
            raise OSError("injected restore failure")
        return real_restore(claim, claim_fd, slug, claim_snapshot)

    monkeypatch.setattr(loader, "_restore_claimed_update", fail_once)
    assert (
        loader.auto_apply_pending_update(
            "restore-failure-update",
            expected_candidate_binding="mismatched-binding",
        )
        is None
    )
    assert not (loader._pending_root() / "restore-failure-update").exists()

    assert [row["slug"] for row in loader.list_pending_skills()] == ["restore-failure-update"]
    assert restore_calls == 2
    claim_locks = loader._locks_root() / "claims"
    evidence = list(loader._evidence_root().iterdir())
    retained_locks = list(claim_locks.glob("*.lock"))
    assert len(evidence) == 1
    assert len(retained_locks) == 1
    assert retained_locks[0].stem == evidence[0].name


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
    real_validate = loader._validate_and_redact_snapshot

    def reject_by_name_validation(*_args, **_kwargs):
        pytest.fail("new approval reopened the mutable claim")

    def blocking_snapshot_validation(tree, name):
        inspection_started.set()
        assert allow_approval.wait(timeout=5)
        return real_validate(tree, name)

    monkeypatch.setattr(loader, "_validate_and_redact_candidate", reject_by_name_validation)
    monkeypatch.setattr(
        loader,
        "_validate_and_redact_snapshot",
        blocking_snapshot_validation,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        approval = pool.submit(loader.approve_pending_skill, "approve-replacement-race")
        assert inspection_started.wait(timeout=5)
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
    pending = unprotected._pending_root() / "unprotected-candidate"
    pending.mkdir(parents=True)
    skill_bytes = b"---\nname: auto/unprotected-candidate\n---\nORIGINAL\n"
    metadata_bytes = b'{"kind":"new","notify_suppressed":false}\r\n'
    (pending / "SKILL.md").write_bytes(skill_bytes)
    (pending / ".meta.json").write_bytes(metadata_bytes)
    assert not os.path.lexists(unprotected._private_root())

    assert unprotected._claim_pending_update("unprotected-candidate") is None

    assert not os.path.lexists(unprotected._private_root())
    assert (pending / "SKILL.md").read_bytes() == skill_bytes
    assert (pending / ".meta.json").read_bytes() == metadata_bytes
    assert {entry.name for entry in pending.parent.parent.iterdir()} == {
        skills_mod.AUTO_PENDING_DIRNAME
    }


def test_skill_lock_files_are_prepared_for_windows(loader, monkeypatch):
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
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
    claim, fd, _consumed_at, claim_snapshot = claimed
    claim_lock = loader._claim_lock_path(claim.name)
    assert loader._authenticated_claim_snapshot_state(fd, claim_lock, claim.name) == claim_snapshot
    loader._restore_claimed_update(claim, fd, "windows-lock-byte", claim_snapshot)
    os.close(fd)
    assert (loader._evidence_root() / claim.name).is_dir()


def test_skill_lock_refuses_hardlink_without_touching_target(loader):
    assert loader._private_state_roots_safe(create=True) is True
    victim = loader._locks_root() / "hardlink-victim"
    victim.write_bytes(b"DO NOT TOUCH")
    lock_path = loader._locks_root() / "hardlinked.lock"
    os.link(victim, lock_path)

    with pytest.raises(OSError):
        _open_lock_for_test(loader, lock_path)

    assert victim.read_bytes() == b"DO NOT TOUCH"
    assert lock_path.read_bytes() == b"DO NOT TOUCH"


def test_windows_private_cleanup_retries_readonly_regular_file(loader, monkeypatch):
    assert loader._private_state_roots_safe(create=True) is True
    target = loader._claims_root() / "readonly-private-file"
    target.write_bytes(b"private")
    real_unlink = skills_mod.os.unlink
    attempts = 0

    def refuse_once(path, *args, **kwargs):
        nonlocal attempts
        if Path(path).name == target.name:
            attempts += 1
            if attempts == 1:
                raise PermissionError(errno.EACCES, "read-only", str(path))
        return real_unlink(path, *args, **kwargs)

    with loader._pin_private_state(create=False) as private_state:
        expected = loader._stat_pinned_child(private_state.claims, target.name)
        monkeypatch.setattr(skills_mod.os, "name", "nt")
        monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            skills_mod.platform_compat,
            "opened_path_identity_matches",
            lambda _fd, _path: True,
        )
        monkeypatch.setattr(skills_mod.os, "unlink", refuse_once)

        assert (
            loader._unlink_skill_child(
                private_state.claims,
                target.name,
                expected=expected,
            )
            is True
        )

    assert attempts == 2
    assert not target.exists()


def test_private_roots_refuse_linked_auto_namespace_ancestor(loader, tmp_path, caplog):
    """A symlinked ``auto`` namespace must fail closed before any mkdir."""
    auto_root = loader._dir / skills_mod.AUTO_SKILL_NAMESPACE
    loader._dir.mkdir(parents=True, exist_ok=True)
    if auto_root.exists():
        shutil.rmtree(auto_root)
    elsewhere = tmp_path / "elsewhere-auto"
    elsewhere.mkdir()
    auto_root.symlink_to(elsewhere, target_is_directory=True)
    try:
        assert loader._private_state_roots_safe(create=True) is False
        # Nothing may be created through the link.
        assert not (elsewhere / skills_mod.AUTO_PRIVATE_DIRNAME).exists()
        assert "link the Kiro Crew data-home root instead" in caplog.text
    finally:
        auto_root.unlink()


def test_private_roots_refuse_linked_pending_ancestor(loader, tmp_path):
    """A linked ``.pending`` sibling must fail closed before private mkdir."""
    pending_root = loader._pending_root()
    pending_root.parent.mkdir(parents=True, exist_ok=True)
    assert not os.path.lexists(loader._private_root())
    elsewhere = tmp_path / "elsewhere-pending"
    elsewhere.mkdir()
    sentinel = elsewhere / "sentinel"
    sentinel.write_text("DO NOT TOUCH", encoding="utf-8")
    skills_mod.platform_compat.symlink_or_junction(elsewhere, pending_root)
    try:
        assert loader._private_state_roots_safe(create=True) is False
        assert loader._claim_pending_update("any-slug") is None
        assert not os.path.lexists(loader._private_root())
        assert sentinel.read_text(encoding="utf-8") == "DO NOT TOUCH"
        assert list(elsewhere.iterdir()) == [sentinel]
    finally:
        skills_mod.platform_compat.unlink_link_or_junction(pending_root)


def test_private_roots_allow_linked_operator_prefix(tmp_path):
    """Links ABOVE the skills dir (operator-controlled) must stay allowed."""
    real_home = tmp_path / "real-home"
    (real_home / "skills").mkdir(parents=True)
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    loader = skills_mod.SkillsLoader(linked_home / "skills", install_builtins=False)
    assert loader._private_state_roots_safe(create=True) is True


def test_private_root_creation_refuses_swapped_auto_parent(
    loader,
    monkeypatch,
    private_state_mode,
):
    loader._dir.mkdir(parents=True, exist_ok=True)
    auto_root = loader._dir / skills_mod.AUTO_SKILL_NAMESPACE
    auto_root.mkdir()
    (auto_root / skills_mod.AUTO_PENDING_DIRNAME).mkdir()
    displaced = loader._dir / "auto-before-swap"
    real_pin_child = loader._pin_skill_child_parent
    swapped = False

    @contextlib.contextmanager
    def swap_before_private_create(parent, name, *, create):
        nonlocal swapped
        if name == skills_mod.AUTO_PRIVATE_DIRNAME and not swapped:
            auto_root.rename(displaced)
            auto_root.mkdir()
            swapped = True
        with real_pin_child(parent, name, create=create) as child:
            yield child

    monkeypatch.setattr(loader, "_pin_skill_child_parent", swap_before_private_create)

    assert loader._private_state_roots_safe(create=True) is False
    # POSIX permits the injected rename and then fails identity revalidation.
    # Windows no-delete-sharing pins may refuse the rename before the hook
    # reaches its assignment; both outcomes prove the replacement is untouched.
    assert swapped is (os.name != "nt")
    if swapped:
        assert list(auto_root.iterdir()) == []
        assert (displaced / skills_mod.AUTO_PENDING_DIRNAME).is_dir()
    else:
        assert not displaced.exists()
        assert {entry.name for entry in auto_root.iterdir()} == {skills_mod.AUTO_PENDING_DIRNAME}
    assert not (auto_root / skills_mod.AUTO_PRIVATE_DIRNAME).exists()


def test_skill_lock_creation_refuses_swapped_locks_parent(
    loader,
    monkeypatch,
    private_state_mode,
):
    assert loader._private_state_roots_safe(create=True) is True
    locks_root = loader._locks_root()
    displaced = locks_root.with_name("locks-before-swap")
    real_open_lock = loader._open_skill_lock
    swapped = False

    def swap_before_lock_create(parent, name):
        nonlocal swapped
        if not swapped:
            locks_root.rename(displaced)
            locks_root.mkdir()
            swapped = True
        return real_open_lock(parent, name)

    monkeypatch.setattr(loader, "_open_skill_lock", swap_before_lock_create)

    with loader._file_lock("ancestor-swap.lock") as acquired:
        assert acquired is False

    # POSIX permits the injected rename and then fails identity revalidation.
    # Windows no-delete-sharing pins may refuse the rename before the hook
    # reaches its assignment; both outcomes prove the replacement is untouched.
    assert swapped is (os.name != "nt")
    if swapped:
        assert list(locks_root.iterdir()) == []
        assert (displaced / skills_mod.AUTO_CLAIMS_DIRNAME).is_dir()
    else:
        assert not displaced.exists()
        assert {entry.name for entry in locks_root.iterdir()} == {skills_mod.AUTO_CLAIMS_DIRNAME}
    assert not (locks_root / "ancestor-swap.lock").exists()


def test_claim_creation_refuses_swapped_claims_parent(
    loader,
    monkeypatch,
    private_state_mode,
):
    slug = "claim-ancestor-swap"
    _stage_update(loader, slug, target="auto/claim-target")
    claims_root = loader._claims_root()
    displaced = claims_root.with_name("claims-before-swap")
    real_rename = loader._rename_skill_child_no_replace
    swapped = False

    def swap_before_claim_create(source, source_name, destination, destination_name):
        nonlocal swapped
        if destination_name.startswith(f"{slug}--") and not swapped:
            claims_root.rename(displaced)
            claims_root.mkdir()
            swapped = True
        return real_rename(source, source_name, destination, destination_name)

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", swap_before_claim_create)

    assert loader._claim_pending_update(slug) is None
    # POSIX permits the injected rename and then fails identity revalidation.
    # Windows no-delete-sharing pins may refuse the rename before the hook
    # reaches its assignment; both outcomes prove the replacement is untouched.
    assert swapped is (os.name != "nt")
    assert list(claims_root.iterdir()) == []
    assert (loader._pending_root() / slug / "SKILL.md").exists()
    assert not list((loader._locks_root() / "claims").glob(f"{slug}--*.lock"))


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
    monkeypatch.setattr(skills_mod, "_DIR_FD_SUPPORTED", False)
    monkeypatch.setattr(skills_mod.os, "open", open_swapped_inode)

    with pytest.raises(OSError):
        _open_lock_for_test(loader, lock_path)

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
        unattended=True,
        unattended_binding_out=binding,
    )
    seen = []
    monkeypatch.setattr(loader, "_claim_pending_update", lambda _slug: None)
    monkeypatch.setattr(loader, "emit_pending_staged", seen.append)

    assert (
        loader.auto_apply_pending_update(
            "claim-notify-update",
            expected_candidate_binding=binding[0],
        )
        is None
    )
    assert seen == ["claim-notify-update"]


def test_failed_prepared_restore_requeues_only_authenticated_snapshot(loader, monkeypatch):
    _write_live(loader, "immutable-restore", version=1, body="OLD")
    slug = "immutable-restore-update"
    _stage_update(
        loader,
        slug,
        target="auto/immutable-restore",
        scripts=[{"filename": "safe.py", "content": "print('safe')\n"}],
    )
    mutated_claims: list[Path] = []

    def mutate_claim_and_refuse(**_kwargs):
        claim = next(loader._claims_root().glob(f"{slug}--*"))
        (claim / "SKILL.md").write_text("MUTATED CLAIM\n", encoding="utf-8")
        scripts = claim / "scripts"
        scripts.mkdir(exist_ok=True)
        (scripts / "late.py").write_text("print('late')\n", encoding="utf-8")
        mutated_claims.append(claim)
        return "incomplete"

    monkeypatch.setattr(loader, "_publish_prepared_skill_tree", mutate_claim_and_refuse)

    assert loader.approve_pending_update(slug) is None

    pending = loader._pending_root() / slug
    assert "new steps" in (pending / "SKILL.md").read_text(encoding="utf-8")
    assert (pending / "scripts" / "safe.py").read_text(encoding="utf-8") == "print('safe')\n"
    assert not (pending / "scripts" / "late.py").exists()
    assert len(mutated_claims) == 1
    claim = mutated_claims[0]
    evidence = loader._evidence_root() / claim.name
    assert not claim.exists()
    assert (evidence / "SKILL.md").read_text(encoding="utf-8") == "MUTATED CLAIM\n"
    assert (evidence / "scripts" / "late.py").read_text(encoding="utf-8") == ("print('late')\n")
    lock_path = loader._claim_lock_path(claim.name)
    fd = _open_lock_for_test(loader, lock_path)
    try:
        assert loader._authenticated_claim_evidence_state(fd, lock_path, claim.name) is not None
    finally:
        os.close(fd)

    assert [row["slug"] for row in loader.list_pending_skills()] == [slug]
    assert evidence.is_dir()
    assert lock_path.is_file()


def test_mutation_after_restore_is_atomically_moved_to_private_evidence(loader, monkeypatch):
    slug = "evidence-retained-mutation"
    _stage_update(loader, slug, target="auto/evidence-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, snapshot = claimed
    entered_transition = threading.Event()
    allow_transition = threading.Event()
    real_rename = loader._rename_skill_child_no_replace

    def mutate_boundary(source, source_name, destination, destination_name):
        if source_name == claim.name and destination.path == loader._evidence_root():
            entered_transition.set()
            assert allow_transition.wait(timeout=2)
        return real_rename(source, source_name, destination, destination_name)

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", mutate_boundary)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(loader._restore_claimed_update, claim, fd, slug, snapshot)
            assert entered_transition.wait(timeout=2)
            (claim / "SKILL.md").write_text("MUTATED AFTER RESTORE\n", encoding="utf-8")
            allow_transition.set()
            assert future.result(timeout=2) == loader._pending_root() / slug

        evidence = loader._evidence_root() / claim.name
        lock_path = loader._claim_lock_path(claim.name)
        assert not claim.exists()
        assert evidence.is_dir()
        assert (evidence / "SKILL.md").read_text(encoding="utf-8") == ("MUTATED AFTER RESTORE\n")
        assert "new steps" in (loader._pending_root() / slug / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert loader._authenticated_claim_evidence_state(fd, lock_path, claim.name) is not None
    finally:
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    loader._cleanup_claim_lock(claim.name)
    assert loader._claim_lock_path(claim.name).is_file()


def test_restart_keeps_evidence_and_never_requeues_it_twice(loader):
    slug = "evidence-restart-once"
    _stage_update(loader, slug, target="auto/evidence-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, snapshot = claimed
    try:
        assert loader._restore_claimed_update(claim, fd, slug, snapshot) == (
            loader._pending_root() / slug
        )
    finally:
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    evidence = loader._evidence_root() / claim.name
    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert [row["slug"] for row in restarted.list_pending_skills()] == [slug]
    assert [row["slug"] for row in restarted.list_pending_skills()] == [slug]
    assert evidence.is_dir()
    assert not (restarted._pending_root() / f"{slug}-2").exists()
    lock_path = restarted._claim_lock_path(claim.name)
    evidence_fd = _open_lock_for_test(restarted, lock_path)
    try:
        assert (
            restarted._authenticated_claim_evidence_state(
                evidence_fd,
                lock_path,
                claim.name,
            )
            is not None
        )
    finally:
        os.close(evidence_fd)


def test_unmodified_claim_uses_the_same_private_evidence_policy(loader):
    slug = "evidence-unmodified"
    _stage_update(loader, slug, target="auto/evidence-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, snapshot = claimed
    original = (claim / "SKILL.md").read_bytes()
    try:
        assert loader._restore_claimed_update(claim, fd, slug, snapshot) == (
            loader._pending_root() / slug
        )
        evidence = loader._evidence_root() / claim.name
        assert not claim.exists()
        assert (evidence / "SKILL.md").read_bytes() == original
        assert (
            loader._authenticated_claim_evidence_state(
                fd,
                loader._claim_lock_path(claim.name),
                claim.name,
            )
            is not None
        )
    finally:
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)


def test_restart_finishes_evidence_move_after_durable_state_commit(loader, monkeypatch):
    slug = "evidence-interrupted-move"
    _stage_update(loader, slug, target="auto/evidence-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, snapshot = claimed
    real_rename = loader._rename_skill_child_no_replace
    refused = False

    def refuse_first_evidence_move(source, source_name, destination, destination_name):
        nonlocal refused
        if destination.path == loader._evidence_root() and not refused:
            refused = True
            raise OSError("injected evidence move interruption")
        return real_rename(source, source_name, destination, destination_name)

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", refuse_first_evidence_move)
    try:
        assert loader._restore_claimed_update(claim, fd, slug, snapshot) == (
            loader._pending_root() / slug
        )
        assert claim.is_dir()
        assert (
            loader._authenticated_claim_evidence_state(
                fd,
                loader._claim_lock_path(claim.name),
                claim.name,
            )
            is not None
        )
    finally:
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert [row["slug"] for row in restarted.list_pending_skills()] == [slug]
    assert not claim.exists()
    assert (restarted._evidence_root() / claim.name).is_dir()
    assert not (restarted._pending_root() / f"{slug}-2").exists()
    assert restarted._claim_lock_path(claim.name).is_file()


def test_restart_uses_restore_receipt_before_evidence_state_commit(loader, monkeypatch):
    slug = "evidence-receipt-recovery"
    _stage_update(loader, slug, target="auto/evidence-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, snapshot = claimed
    real_commit = loader._commit_claim_evidence_state
    refused = False

    def refuse_first_commit(*args, **kwargs):
        nonlocal refused
        if not refused:
            refused = True
            return False
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(loader, "_commit_claim_evidence_state", refuse_first_commit)
    try:
        assert loader._restore_claimed_update(claim, fd, slug, snapshot) == (
            loader._pending_root() / slug
        )
        assert claim.is_dir()
        assert (
            loader._authenticated_claim_restore_state(
                fd,
                loader._claim_lock_path(claim.name),
                claim.name,
            )
            is not None
        )
    finally:
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert [row["slug"] for row in restarted.list_pending_skills()] == [slug]
    assert not claim.exists()
    assert (restarted._evidence_root() / claim.name).is_dir()
    assert not (restarted._pending_root() / f"{slug}-2").exists()


def test_evidence_name_collision_retains_both_trees_without_requeue(loader):
    slug = "evidence-name-collision"
    _stage_update(loader, slug, target="auto/evidence-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, snapshot = claimed
    collision = loader._evidence_root() / claim.name
    collision.mkdir()
    (collision / "sentinel").write_text("DO NOT TOUCH", encoding="utf-8")
    try:
        assert loader._restore_claimed_update(claim, fd, slug, snapshot) == (
            loader._pending_root() / slug
        )
        assert claim.is_dir()
        assert (collision / "sentinel").read_text(encoding="utf-8") == "DO NOT TOUCH"
        assert (
            loader._authenticated_claim_evidence_state(
                fd,
                loader._claim_lock_path(claim.name),
                claim.name,
            )
            is not None
        )
    finally:
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert [row["slug"] for row in restarted.list_pending_skills()] == [slug]
    assert claim.is_dir()
    assert (collision / "sentinel").read_text(encoding="utf-8") == "DO NOT TOUCH"
    assert not (restarted._pending_root() / f"{slug}-2").exists()
    assert restarted._claim_lock_path(claim.name).is_file()


def test_recovery_migrates_existing_private_hierarchy_without_evidence(loader):
    assert loader._private_state_roots_safe(create=True) is True
    evidence = loader._evidence_root()
    evidence.rmdir()
    assert not evidence.exists()

    loader._recover_abandoned_claims()

    assert evidence.is_dir()
    assert loader._private_state_roots_safe(create=False) is True


def test_recovery_refuses_linked_legacy_evidence_namespace(loader, tmp_path):
    assert loader._private_state_roots_safe(create=True) is True
    evidence = loader._evidence_root()
    evidence.rmdir()
    outside = tmp_path / "outside-evidence"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("DO NOT TOUCH", encoding="utf-8")
    skills_mod.platform_compat.symlink_or_junction(outside, evidence)

    loader._recover_abandoned_claims()

    assert sentinel.read_text(encoding="utf-8") == "DO NOT TOUCH"
    assert sorted(child.name for child in outside.iterdir()) == ["sentinel"]


def test_rolled_back_cleanup_failure_is_not_reported_fully_live(loader, monkeypatch, caplog):
    slug = "rollback-cleanup-state"
    _stage_update(loader, slug, target="auto/rollback-cleanup-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, snapshot = claimed
    monkeypatch.setattr(
        loader, "_authenticated_claim_publication", lambda *_args: {"kind": "update"}
    )
    monkeypatch.setattr(loader, "_reconcile_prepared_claim", lambda *_args: False)
    monkeypatch.setattr(loader, "_cleanup_publication_artifacts", lambda _name: False)
    monkeypatch.setattr(
        loader,
        "_restore_claimed_update",
        lambda *_args: pytest.fail("cleanup failure must retain the rolled-back claim"),
    )
    try:
        with caplog.at_level("INFO"):
            assert loader._restore_failed_promotion_claim(claim, fd, slug, snapshot) is False
    finally:
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    assert "rolled back but artifact cleanup failed" in caplog.text
    assert "fully live" not in caplog.text
    assert claim.is_dir()
    assert loader._claim_lock_path(claim.name).is_file()


@pytest.mark.parametrize(
    ("suppressed", "expected"),
    [
        (True, ["strict-notify-update"]),
        (False, []),
        ("true", []),
        ("false", []),
    ],
)
def test_restore_notification_suppression_requires_literal_true(
    loader, monkeypatch, suppressed, expected
):
    slug = "strict-notify-update"
    _stage_update(loader, slug, target="auto/missing-target")
    metadata_path = loader._pending_root() / slug / ".meta.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["notify_suppressed"] = suppressed
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    seen: list[str] = []
    monkeypatch.setattr(
        loader,
        "_emit_pending_staged_metadata",
        lambda staged_slug, _meta: seen.append(staged_slug),
    )

    assert loader.approve_pending_update(slug) is None
    assert seen == expected


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
    real_validate = loader._validate_and_redact_snapshot
    count_lock = threading.Lock()
    claim_count = 0
    validation_count = 0

    def recording_claim(candidate_slug):
        nonlocal claim_count
        result = real_claim(candidate_slug)
        if result is not None:
            with count_lock:
                claim_count += 1
                if claim_count == 2:
                    second_claimed.set()
        return result

    def blocking_first_validation(tree, name):
        nonlocal validation_count
        with count_lock:
            validation_count += 1
            current = validation_count
        if current == 1:
            first_inspection.set()
            assert allow_first.wait(timeout=5)
        else:
            unexpected_second_inspection.set()
        return real_validate(tree, name)

    monkeypatch.setattr(loader, "_claim_pending_update", recording_claim)
    monkeypatch.setattr(
        loader,
        "_validate_and_redact_snapshot",
        blocking_first_validation,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(loader.approve_pending_skill, slug)
        assert first_inspection.wait(timeout=5)
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
    pending_before_claim = loader._pending_root() / slug
    original_skill = (pending_before_claim / "SKILL.md").read_bytes()
    metadata_file = pending_before_claim / ".meta.json"
    normalized_metadata = metadata_file.read_bytes().replace(b"\r\n", b"\n")
    original_metadata = normalized_metadata.replace(b"\n", b"\r\n")
    metadata_file.write_bytes(original_metadata)
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, snapshot = claimed
    pending = loader._pending_root() / slug
    entered_restore = threading.Event()
    allow_restore = threading.Event()
    real_rename = loader._rename_skill_child_no_replace
    blocked = False

    restore_stage_name = loader._restoration_path(claim.name).name

    def blocking_rename(source, source_name, destination, destination_name):
        nonlocal blocked
        if source_name == restore_stage_name and destination_name == slug and not blocked:
            blocked = True
            entered_restore.set()
            assert allow_restore.wait(timeout=2)
        return real_rename(source, source_name, destination, destination_name)

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", blocking_rename)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(loader._restore_claimed_update, claim, fd, slug, snapshot)
        assert entered_restore.wait(timeout=2)
        pending.mkdir()
        (pending / "SKILL.md").write_text("DIRECT REPLACEMENT", encoding="utf-8")
        allow_restore.set()
        restored = future.result(timeout=2)
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    assert restored is not None and restored.name == f"{slug}-2"
    assert (pending / "SKILL.md").read_text(encoding="utf-8") == "DIRECT REPLACEMENT"
    assert (restored / "SKILL.md").read_bytes() == original_skill
    restored_metadata = json.loads((restored / ".meta.json").read_bytes())
    assert restored_metadata["slug"] == f"{slug}-2"
    assert restored_metadata["name"] == f"auto/{slug}-2"
    assert restored_metadata["notify_suppressed"] is False
    evidence = loader._evidence_root() / claim.name
    assert evidence.is_dir()
    assert (evidence / "SKILL.md").read_bytes() == original_skill
    assert (evidence / ".meta.json").read_bytes() == original_metadata
    assert loader._claim_lock_path(claim.name).is_file()


def test_claim_refuses_before_public_move_when_no_replace_is_unsupported(
    loader, monkeypatch, caplog
):
    slug = "unsupported-no-replace"
    _stage_update(loader, slug, target="auto/no-replace-target")
    pending = loader._pending_root() / slug

    def unsupported(_source, _source_name, _destination, _destination_name):
        raise OSError(errno.ENOTSUP, "atomic no-replace rename unavailable")

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", unsupported)

    assert loader._claim_pending_update(slug) is None
    assert pending.is_dir()
    assert "new steps" in (pending / "SKILL.md").read_text(encoding="utf-8")
    assert not list(loader._claims_root().glob(f"{slug}--*"))
    assert not list((loader._locks_root() / "claims").glob(f"{slug}--*.lock"))
    assert "Move KIROCREW_HOME to a local filesystem" in caplog.text
    assert "renameat2(RENAME_NOREPLACE)" in caplog.text


def test_partial_cleanup_marker_repair_failure_uses_completed_lock_state(loader, monkeypatch):
    _write_live(loader, "lock-outcome-recovery", version=1, body="OLD")
    slug = "lock-outcome-recovery-update"
    _stage_update(loader, slug, target="auto/lock-outcome-recovery")
    real_remove_tree = loader._remove_private_tree
    real_write_marker = loader._write_completion_marker
    cleanup_failed = False
    marker_writes = 0

    def remove_marker_then_fail(path, *, what, expected_identity=None):
        nonlocal cleanup_failed
        candidate = Path(path)
        if candidate.parent == loader._claims_root() and not cleanup_failed:
            cleanup_failed = True
            assert loader._authenticated_completion_marker(candidate) is True
            assert loader._remove_untrusted_completion_marker(candidate) is True
            return False
        return real_remove_tree(path, what=what, expected_identity=expected_identity)

    def write_initial_marker_only(candidate, expected_identity=None):
        nonlocal marker_writes
        marker_writes += 1
        if marker_writes == 1:
            return real_write_marker(candidate, expected_identity)
        return False

    monkeypatch.setattr(loader, "_remove_private_tree", remove_marker_then_fail)
    monkeypatch.setattr(loader, "_write_completion_marker", write_initial_marker_only)

    assert loader.approve_pending_update(slug) == "auto/lock-outcome-recovery"
    leftovers = list(loader._claims_root().glob(f"{slug}--*"))
    assert len(leftovers) == 1
    claim = leftovers[0]
    assert loader._authenticated_completion_marker(claim) is False

    lock_path = loader._claim_lock_path(claim.name)
    fd = _open_lock_for_test(loader, lock_path)
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
    real_remove_tree = loader._remove_private_tree
    failed = False

    def partially_remove_first_claim(path, *, what, expected_identity=None):
        nonlocal failed
        candidate = Path(path)
        if candidate.parent == loader._claims_root() and not failed:
            failed = True
            assert loader._authenticated_completion_marker(candidate) is True
            assert loader._remove_untrusted_completion_marker(candidate) is True
            return False
        return real_remove_tree(path, what=what, expected_identity=expected_identity)

    monkeypatch.setattr(loader, "_remove_private_tree", partially_remove_first_claim)

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
    claim, fd, _consumed_at, _generation = claimed
    assert loader._write_completion_marker(claim) is True
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)
    real_remove_tree = loader._remove_private_tree
    failed = False

    def partially_remove_first_claim(path, *, what, expected_identity=None):
        nonlocal failed
        candidate = Path(path)
        if candidate == claim and not failed:
            failed = True
            assert loader._authenticated_completion_marker(candidate) is True
            assert loader._remove_untrusted_completion_marker(candidate) is True
            return False
        return real_remove_tree(path, what=what, expected_identity=expected_identity)

    monkeypatch.setattr(loader, "_remove_private_tree", partially_remove_first_claim)

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
    real_remove_tree = loader._remove_private_tree
    failed = False

    def fail_first_claim_cleanup(path, *, what, expected_identity=None):
        nonlocal failed
        candidate = Path(path)
        if candidate.parent == loader._claims_root() and not failed:
            failed = True
            assert loader._authenticated_completion_marker(candidate) is True
            assert loader._remove_untrusted_completion_marker(candidate) is True
            return False
        return real_remove_tree(path, what=what, expected_identity=expected_identity)

    monkeypatch.setattr(loader, "_remove_private_tree", fail_first_claim_cleanup)

    assert loader.dismiss_pending_skill(slug) is True
    leftovers = list(loader._claims_root().iterdir())
    assert len(leftovers) == 1
    assert loader._authenticated_completion_marker(leftovers[0]) is True
    assert loader.list_pending_skills() == []
    assert not loader._claims_root().exists() or not list(loader._claims_root().iterdir())


def test_pending_slug_claim_requires_exact_double_hyphen_prefix(loader):
    assert loader._private_state_roots_safe(create=True) is True
    claim = loader._claims_root() / "foo--x--0123456789abcdef"
    claim.mkdir()

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
    real_rename = loader._rename_skill_child_no_replace

    def blocking_rename(source, source_name, destination, destination_name):
        if source_name == pending.name:
            entered_rename.set()
            assert allow_rename.wait(timeout=2)
        return real_rename(source, source_name, destination, destination_name)

    def claim():
        claim_started.set()
        return loader._claim_pending_update("dismiss-race-update")

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", blocking_rename)
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
    claim, fd, _consumed_at, claim_snapshot = claimed
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

    monkeypatch.setattr(skills_mod, "is_link_or_junction", fake_is_link_or_junction)
    monkeypatch.setattr(path_type, "read_text", guarded_read_text)

    try:
        loader._restore_claimed_update(claim, fd, "symlinked-meta-update", claim_snapshot)
    finally:
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    assert (loader._pending_root() / "symlinked-meta-update").is_dir()
    assert (loader._evidence_root() / claim.name).is_dir()


@pytest.mark.parametrize(
    "flow",
    ["update", "new", "dismiss", "reserved-marker", "restart"],
)
@pytest.mark.parametrize("mutation", ["regular-swap", "symlink", "reparse"])
def test_every_refusal_and_restart_path_uses_claimed_metadata(
    loader, monkeypatch, tmp_path, flow, mutation
):
    if mutation == "symlink" and os.name == "nt":
        pytest.skip("file-symlink creation needs Windows developer mode")
    slug = f"captured-{flow}-{mutation}"
    if flow == "new":
        loader.stage_skill_candidate(
            slug,
            description="original description",
            triggers="original-trigger",
            procedure_md="## Steps\n\nORIGINAL",
            provenance=_prov(),
            notify=False,
        )
        expected_target = None
    else:
        _stage_update(
            loader,
            slug,
            target="auto/original-target",
            notify=False,
        )
        expected_target = "auto/original-target"
    pending = loader._pending_root() / slug
    if flow == "reserved-marker":
        (pending / ".promoted").write_text("reserved\n", encoding="utf-8")

    secret_file = tmp_path / f"{slug}-credential.json"
    secret_file.write_text('{"target":"auto/credential-target"}', encoding="utf-8")
    secret_dir = tmp_path / f"{slug}-credential-dir"
    secret_dir.mkdir()
    sentinel = secret_dir / "sentinel"
    sentinel.write_text("DO NOT TOUCH", encoding="utf-8")
    real_rename = loader._rename_skill_child_no_replace

    def rename_then_swap(source, source_name, destination, destination_name):
        result = real_rename(source, source_name, destination, destination_name)
        if source_name != slug:
            return result
        metadata = destination.path / destination_name / ".meta.json"
        metadata.unlink()
        if mutation == "regular-swap":
            metadata.write_text(
                json.dumps(
                    {
                        "slug": slug,
                        "name": f"auto/{slug}",
                        "kind": "update",
                        "target": "auto/credential-target",
                        "description": "SWAPPED",
                        "notify_suppressed": False,
                    }
                ),
                encoding="utf-8",
            )
        elif mutation == "symlink":
            os.symlink(secret_file, metadata)
        else:
            skills_mod.platform_compat.symlink_or_junction(secret_dir, metadata)
        return result

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", rename_then_swap)

    if flow == "update":
        assert loader.approve_pending_update(slug) is None
    elif flow == "new":
        assert loader.approve_pending_skill(slug) is None
    elif flow == "dismiss":
        monkeypatch.setattr(loader, "_write_completion_marker", lambda _claim: False)
        assert loader.dismiss_pending_skill(slug) is False
    elif flow == "reserved-marker":
        assert loader.approve_pending_update(slug) is None
    else:
        assert loader._claim_pending_update(slug) is None
        assert [row["slug"] for row in loader.list_pending_skills()] == [slug]

    restored_meta_path = loader._pending_root() / slug / ".meta.json"
    assert restored_meta_path.is_file()
    assert not skills_mod.platform_compat.is_link_or_junction(restored_meta_path)
    restored_meta = json.loads(restored_meta_path.read_text(encoding="utf-8"))
    expected_description = "original description" if flow == "new" else f"updated {slug}"
    assert restored_meta["description"] == expected_description
    assert restored_meta.get("target") == expected_target
    assert restored_meta["slug"] == slug
    assert restored_meta["name"] == f"auto/{slug}"
    assert restored_meta["notify_suppressed"] is True
    assert secret_file.read_text(encoding="utf-8") == '{"target":"auto/credential-target"}'
    assert sentinel.read_text(encoding="utf-8") == "DO NOT TOUCH"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permits renaming an opened parent")
@pytest.mark.parametrize(
    "phase",
    ["claim-pending", "claim-claims", "restore-pending", "restore-claims"],
)
@pytest.mark.parametrize("replacement_kind", ["directory", "symlink", "none"])
def test_claim_and_restore_revalidate_captured_parent_matrix(
    loader,
    monkeypatch,
    tmp_path,
    phase,
    replacement_kind,
):
    """A swapped source or destination parent can only refuse the operation."""
    slug = f"parent-swap-{phase}-{replacement_kind}"
    _stage_update(loader, slug, target="auto/parent-swap-target")
    claim = None
    claim_fd = None
    claim_snapshot = None
    if phase.startswith("restore"):
        claimed = loader._claim_pending_update(slug)
        assert claimed is not None
        claim, claim_fd, _consumed_at, claim_snapshot = claimed

    real_rename = loader._rename_skill_child_no_replace
    swapped: dict[str, Path] = {}
    sentinel_path: list[Path] = []

    def swap_parent(parent: Path) -> None:
        detached = parent.with_name(f"{parent.name}-detached-{secrets.token_hex(4)}")
        parent.rename(detached)
        replacement = tmp_path / f"protected-{phase}-{replacement_kind}"
        protected_claim = replacement / (claim.name if claim is not None else "profiles")
        protected_claim.mkdir(parents=True)
        sentinel = protected_claim / "sentinel"
        sentinel.write_text("DO NOT TOUCH", encoding="utf-8")
        if replacement_kind == "symlink":
            parent.symlink_to(replacement, target_is_directory=True)
        else:
            replacement.rename(parent)
            sentinel = parent / protected_claim.relative_to(replacement) / "sentinel"
        swapped["detached"] = detached
        sentinel_path.append(sentinel)

    def swap_then_rename(source, source_name, destination, destination_name):
        should_swap = (
            replacement_kind != "none"
            and not swapped
            and (
                (phase == "claim-pending" and source_name == slug)
                or (phase == "claim-claims" and source_name == slug)
                or (
                    phase == "restore-pending"
                    and claim is not None
                    and source_name == loader._restoration_path(claim.name).name
                )
                or (
                    phase == "restore-claims"
                    and claim is not None
                    and source_name == loader._restoration_path(claim.name).name
                )
            )
        )
        if should_swap:
            if phase in {"claim-pending", "restore-pending"}:
                parent = source.path if phase == "claim-pending" else destination.path
            else:
                parent = destination.path if phase == "claim-claims" else source.path
            swap_parent(parent)
        return real_rename(source, source_name, destination, destination_name)

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", swap_then_rename)

    if phase.startswith("claim"):
        result = loader._claim_pending_update(slug)
        if replacement_kind == "none":
            assert result is not None
            claimed_path, claimed_fd, _consumed_at, snapshot = result
            try:
                assert (
                    loader._restore_claimed_update(
                        claimed_path,
                        claimed_fd,
                        slug,
                        snapshot,
                    )
                    is not None
                )
            finally:
                skills_mod.platform_compat.release_lock(claimed_fd)
                os.close(claimed_fd)
        else:
            assert result is None
    else:
        assert claim is not None and claim_fd is not None and claim_snapshot is not None
        try:
            restored = loader._restore_claimed_update(
                claim,
                claim_fd,
                slug,
                claim_snapshot,
            )
        finally:
            skills_mod.platform_compat.release_lock(claim_fd)
            os.close(claim_fd)
        if replacement_kind == "none":
            assert restored == loader._pending_root() / slug
        else:
            assert restored is None

    if replacement_kind != "none":
        assert sentinel_path[0].read_text(encoding="utf-8") == "DO NOT TOUCH"
        assert list(sentinel_path[0].parent.iterdir()) == [sentinel_path[0]]
        detached = swapped["detached"]
        if phase == "claim-pending":
            assert (detached / slug / "SKILL.md").is_file()
        elif phase == "claim-claims":
            assert (loader._pending_root() / slug / "SKILL.md").is_file()
        elif phase == "restore-pending":
            assert claim is not None and claim.is_dir()
        else:
            assert any(child.name.startswith(f"{slug}--") for child in detached.iterdir())


@pytest.mark.skipif(os.name == "nt", reason="POSIX permits renaming an opened parent")
@pytest.mark.parametrize("replacement_kind", ["directory", "symlink", "none"])
def test_completed_claim_delete_revalidates_parent_matrix(
    loader,
    monkeypatch,
    tmp_path,
    replacement_kind,
):
    """Cleanup deletes the captured claim or nothing, never a replacement tree."""
    slug = f"delete-parent-swap-{replacement_kind}"
    _stage_update(loader, slug, target="auto/delete-parent-swap")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, claim_fd, _consumed_at, _snapshot = claimed
    assert loader._write_completion_marker(claim) is True
    assert (
        loader._commit_claim_lock_state(
            claim_fd,
            loader._claim_lock_path(claim.name),
            claim.name,
        )
        is True
    )

    real_remove = skills_mod.pinned_fs.remove_tree_pinned
    swapped: dict[str, Path] = {}
    sentinel_path: list[Path] = []

    def swap_then_remove(resolved_path, **kwargs):
        if replacement_kind != "none" and not swapped:
            claims_root = loader._claims_root()
            detached = claims_root.with_name(f"{claims_root.name}-detached-{secrets.token_hex(4)}")
            claims_root.rename(detached)
            replacement = tmp_path / f"protected-delete-{replacement_kind}"
            protected_claim = replacement / claim.name
            protected_claim.mkdir(parents=True)
            sentinel = protected_claim / "sentinel"
            sentinel.write_text("DO NOT TOUCH", encoding="utf-8")
            if replacement_kind == "symlink":
                claims_root.symlink_to(replacement, target_is_directory=True)
            else:
                replacement.rename(claims_root)
                sentinel = claims_root / claim.name / "sentinel"
            swapped["detached"] = detached
            sentinel_path.append(sentinel)
        return real_remove(resolved_path, **kwargs)

    monkeypatch.setattr(skills_mod.pinned_fs, "remove_tree_pinned", swap_then_remove)
    try:
        cleaned = loader._cleanup_completed_claim(claim, claim_fd)
    finally:
        skills_mod.platform_compat.release_lock(claim_fd)
        os.close(claim_fd)

    if replacement_kind == "none":
        assert cleaned is True
        assert not claim.exists()
    else:
        assert cleaned is False
        sentinel = sentinel_path[0]
        assert sentinel.read_text(encoding="utf-8") == "DO NOT TOUCH"
        assert list(sentinel.parent.iterdir()) == [sentinel]
        assert not (sentinel.parent / ".promoted").exists()
        assert (swapped["detached"] / claim.name / "SKILL.md").is_file()


def test_spoofed_abandoned_completion_marker_restores_claim(loader):
    _write_live(loader, "spoofed-recovery", version=1, body="OLD")
    _stage_update(
        loader,
        "spoofed-recovery-update",
        target="auto/spoofed-recovery",
    )
    claimed = loader._claim_pending_update("spoofed-recovery-update")
    assert claimed is not None
    claim, fd, _consumed_at, _generation = claimed
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
    claim, fd, _consumed_at, _generation = claimed
    assert loader._write_completion_marker(claim) is True
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    assert loader.list_pending_skills() == []
    assert not claim.exists()
    claim_locks = loader._locks_root() / "claims"
    assert not claim_locks.exists() or not list(claim_locks.glob("*.lock"))


def test_restart_recovers_crash_before_live_publish_without_duplicate(loader, monkeypatch):
    """Prepared-before-publish restores once and removes its orphan snapshot."""
    _write_live(loader, "crash-before", version=1, body="OLD")
    slug = "crash-before-update"
    _stage_update(
        loader,
        slug,
        target="auto/crash-before",
        base_version=None,
        scripts=[{"filename": "run.py", "content": "print('after')\n"}],
    )
    live = loader._dir / "auto" / "crash-before"
    real_rename = skills_mod.platform_compat.rename_no_replace

    class InjectedCrash(BaseException):
        pass

    def crash_at_generation_publish(source, destination):
        if Path(destination) == live and Path(source).name.startswith(".publish-"):
            raise InjectedCrash()
        return real_rename(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(
            skills_mod.platform_compat,
            "rename_no_replace",
            crash_at_generation_publish,
        )
        # A real process exit does not execute the in-process refusal restore.
        patch.setattr(loader, "_restore_claimed_update", lambda _claim, _slug, *_rest: None)
        with pytest.raises(InjectedCrash):
            loader.approve_pending_update(slug)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert [row["slug"] for row in restarted.list_pending_skills()] == [slug]
    assert [row["slug"] for row in restarted.list_pending_skills()] == [slug]
    assert (restarted._pending_root() / slug / "scripts" / "run.py").read_text(
        encoding="utf-8"
    ) == "print('after')\n"
    assert restarted.get_auto_skill_version("auto/crash-before") == 1
    versions = restarted._versions_root("crash-before")
    assert not versions.exists() or not list(versions.glob("v*-SKILL.md"))
    assert not restarted._claims_root().exists() or not list(restarted._claims_root().iterdir())


def test_restart_requeues_prepared_new_skill_when_live_target_is_missing(loader, monkeypatch):
    slug = "new-recovery-missing"
    loader.stage_skill_candidate(
        slug,
        description="new recovery",
        triggers="new recovery",
        procedure_md="## Steps\n\nNEW",
        provenance=_prov(),
    )
    live_dir = loader._dir / "auto" / slug
    real_rename = skills_mod.platform_compat.rename_no_replace

    class InjectedCrash(BaseException):
        pass

    def crash_before_new_publish(source, destination):
        if Path(source).name.startswith(".publish-") and Path(destination) == live_dir:
            raise InjectedCrash()
        return real_rename(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(
            skills_mod.platform_compat,
            "rename_no_replace",
            crash_before_new_publish,
        )
        patch.setattr(loader, "_restore_failed_promotion_claim", lambda *_args: None)
        with pytest.raises(InjectedCrash):
            loader.approve_pending_skill(slug)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)

    assert [row["slug"] for row in restarted.list_pending_skills()] == [slug]
    assert [row["slug"] for row in restarted.list_pending_skills()] == [slug]
    assert not live_dir.exists()
    assert not restarted._claims_root().exists() or not list(restarted._claims_root().iterdir())


def test_restart_preserves_concurrent_new_skill_live_target(loader, monkeypatch):
    slug = "new-recovery-occupied"
    loader.stage_skill_candidate(
        slug,
        description="new recovery",
        triggers="new recovery",
        procedure_md="## Steps\n\nNEW",
        provenance=_prov(),
    )
    live_dir = loader._dir / "auto" / slug
    real_rename = skills_mod.platform_compat.rename_no_replace

    class InjectedCrash(BaseException):
        pass

    def crash_before_new_publish(source, destination):
        if Path(source).name.startswith(".publish-") and Path(destination) == live_dir:
            raise InjectedCrash()
        return real_rename(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(
            skills_mod.platform_compat,
            "rename_no_replace",
            crash_before_new_publish,
        )
        patch.setattr(loader, "_restore_failed_promotion_claim", lambda *_args: None)
        with pytest.raises(InjectedCrash):
            loader.approve_pending_skill(slug)

    _write_live(loader, slug, version=9, body="CONCURRENT NEW TARGET")
    claims = list(loader._claims_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    stage, _backup = loader._publication_paths(claims[0].name)
    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)

    assert restarted.list_pending_skills() == []
    assert "CONCURRENT NEW TARGET" in (live_dir / "SKILL.md").read_text(encoding="utf-8")
    assert restarted.get_auto_skill_version(f"auto/{slug}") == 9
    assert claims[0].is_dir()
    assert restarted._skill_tree_hash(stage) is not None


def test_restart_consumes_crash_after_live_publish_without_resurrection(loader, monkeypatch):
    """A legacy update published before completion is never re-queued or reapplied."""
    _write_live(loader, "crash-after", version=1, body="OLD")
    slug = "crash-after-update"
    _stage_update(
        loader,
        slug,
        target="auto/crash-after",
        base_version=None,
        scripts=[{"filename": "run.py", "content": "print('after')\n"}],
    )

    class InjectedCrash(BaseException):
        pass

    with monkeypatch.context() as patch:
        patch.setattr(
            loader,
            "_write_completion_marker",
            lambda _claim: (_ for _ in ()).throw(InjectedCrash()),
        )
        with pytest.raises(InjectedCrash):
            loader.approve_pending_update(slug)

    assert loader.get_auto_skill_version("auto/crash-after") == 2
    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert restarted.list_pending_skills() == []
    assert restarted.get_auto_skill_version("auto/crash-after") == 2
    live_script = restarted._dir / "auto" / "crash-after" / "scripts" / "run.py"
    assert live_script.read_text(encoding="utf-8") == "print('after')\n"
    if os.name != "nt":
        assert live_script.stat().st_mode & 0o111
    snapshots = list(restarted._versions_root("crash-after").glob("v*-SKILL.md"))
    assert [path.name for path in snapshots] == ["v1-SKILL.md"]
    assert not restarted._claims_root().exists() or not list(restarted._claims_root().iterdir())


@pytest.mark.parametrize(
    "mutation",
    ["script-bytes", "script-missing", "script-mode", "metadata", "version"],
)
def test_restart_retains_ambiguous_live_generation_without_discard(loader, monkeypatch, mutation):
    target = f"partial-{mutation}"
    slug = f"{target}-update"
    live_dir = _write_live(loader, target, version=1, body="OLD")
    live_scripts = live_dir / "scripts"
    live_scripts.mkdir()
    old_script = live_scripts / "run.py"
    old_script.write_text("print('before')\n", encoding="utf-8")
    before_hash = loader._skill_tree_hash(live_dir)
    _stage_update(
        loader,
        slug,
        target=f"auto/{target}",
        scripts=[{"filename": "run.py", "content": "print('after')\n"}],
    )

    class InjectedCrash(BaseException):
        pass

    with monkeypatch.context() as patch:
        patch.setattr(
            loader,
            "_write_completion_marker",
            lambda _claim: (_ for _ in ()).throw(InjectedCrash()),
        )
        with pytest.raises(InjectedCrash):
            loader.approve_pending_update(slug)

    published_script = live_dir / "scripts" / "run.py"
    assert loader.get_auto_skill_version(f"auto/{target}") == 2
    if mutation == "script-bytes":
        published_script.write_text("print('partial')\n", encoding="utf-8")
    elif mutation == "script-missing":
        published_script.unlink()
    elif mutation == "script-mode":
        current = stat.S_IMODE(published_script.stat().st_mode)
        if os.name == "nt":
            os.chmod(published_script, stat.S_IREAD if current & stat.S_IWRITE else stat.S_IWRITE)
        else:
            os.chmod(published_script, current ^ stat.S_IXUSR)
    else:
        live_skill = live_dir / "SKILL.md"
        body = live_skill.read_text(encoding="utf-8")
        if mutation == "metadata":
            body = body.replace("created_at: 2020-01-01", "created_at: 2030-01-01")
        else:
            body = body.replace("version: 2", "version: 99")
        live_skill.write_text(body, encoding="utf-8")

    ambiguous_hash = loader._skill_tree_hash(live_dir)
    assert ambiguous_hash is not None and ambiguous_hash != before_hash
    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)

    assert restarted.list_pending_skills() == []
    assert restarted.list_pending_skills() == []
    assert restarted._skill_tree_hash(live_dir) == ambiguous_hash
    claims = list(restarted._claims_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    stage, backup = restarted._publication_paths(claims[0].name)
    assert not stage.exists()
    assert restarted._skill_tree_hash(backup) == before_hash


def test_recovery_retains_live_edit_landing_after_hash_snapshot(loader, monkeypatch):
    target = "recovery-hash-race"
    slug = f"{target}-update"
    live_dir = _write_live(loader, target, version=1, body="OLD")
    before_hash = loader._skill_tree_hash(live_dir)
    _stage_update(loader, slug, target=f"auto/{target}")

    class InjectedCrash(BaseException):
        pass

    with monkeypatch.context() as patch:
        patch.setattr(
            loader,
            "_write_completion_marker",
            lambda _claim: (_ for _ in ()).throw(InjectedCrash()),
        )
        with pytest.raises(InjectedCrash):
            loader.approve_pending_update(slug)

    marker = live_dir / "concurrent-edit"
    marker.write_text("before hash\n", encoding="utf-8")
    claims = list(loader._claims_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    _stage, backup = loader._publication_paths(claims[0].name)
    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    real_hash = restarted._skill_tree_hash
    edit_landed = False

    def hash_then_edit(path):
        nonlocal edit_landed
        result = real_hash(path)
        if Path(path) == live_dir and not edit_landed:
            marker.write_text("after hash\n", encoding="utf-8")
            edit_landed = True
        return result

    monkeypatch.setattr(restarted, "_skill_tree_hash", hash_then_edit)

    assert restarted.list_pending_skills() == []
    assert edit_landed is True
    assert marker.read_text(encoding="utf-8") == "after hash\n"
    assert claims[0].is_dir()
    assert real_hash(backup) == before_hash


def test_recovery_no_replace_preserves_concurrent_recreated_live_target(loader, monkeypatch):
    target = "recovery-recreated-live"
    slug = f"{target}-update"
    live_dir = _write_live(loader, target, version=1, body="OLD")
    before_hash = loader._skill_tree_hash(live_dir)
    _stage_update(loader, slug, target=f"auto/{target}")
    real_rename = skills_mod.platform_compat.rename_no_replace

    class InjectedCrash(BaseException):
        pass

    def crash_before_stage_publish(source, destination):
        if Path(source).name.startswith(".publish-") and Path(destination) == live_dir:
            raise InjectedCrash()
        return real_rename(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(
            skills_mod.platform_compat,
            "rename_no_replace",
            crash_before_stage_publish,
        )
        patch.setattr(loader, "_restore_failed_promotion_claim", lambda *_args: None)
        with pytest.raises(InjectedCrash):
            loader.approve_pending_update(slug)

    claims = list(loader._claims_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    stage, backup = loader._publication_paths(claims[0].name)
    assert not live_dir.exists()
    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    writer_landed = False

    def writer_wins_restore(source, destination):
        nonlocal writer_landed
        if Path(source) == backup and Path(destination) == live_dir and not writer_landed:
            _write_live(loader, target, version=7, body="CONCURRENT")
            writer_landed = True
        return real_rename(source, destination)

    monkeypatch.setattr(
        skills_mod.platform_compat,
        "rename_no_replace",
        writer_wins_restore,
    )

    assert restarted.list_pending_skills() == []
    assert writer_landed is True
    assert "CONCURRENT" in (live_dir / "SKILL.md").read_text(encoding="utf-8")
    assert restarted.get_auto_skill_version(f"auto/{target}") == 7
    assert claims[0].is_dir()
    assert restarted._skill_tree_hash(stage) is not None
    assert restarted._skill_tree_hash(backup) == before_hash


# ── reserved namespace and claimed-inode hardening ──


@pytest.mark.parametrize(
    "name",
    [
        "auto",
        "AUTO",
        "Auto",
        "aUtO",
        # Win32 strips trailing dots/spaces from path components, so these
        # aliases open the ``auto`` directory on Windows filesystems.
        "auto.",
        "auto ",
        "auto. .",
        "AUTO.",
    ],
)
def test_delete_refuses_bare_reserved_auto_namespace(loader, monkeypatch, name):
    live_dir = _write_live(loader, "namespace-survivor", version=1, body="KEEP")
    audit_events: list[dict] = []

    class AuditSink:
        def log_tool_invocation(self, **kwargs):
            audit_events.append(kwargs)

    def unexpected_delete(_name):
        pytest.fail("reserved auto namespace reached the recursive delete helper")

    monkeypatch.setattr(skills_mod, "sel", lambda: AuditSink())
    monkeypatch.setattr(loader, "_delete_skill_unlocked", unexpected_delete)

    assert loader.delete_skill(name) is False
    assert (live_dir / "SKILL.md").exists()
    assert audit_events == [
        {
            "session_key": "skills",
            "tool_name": "skill_mutation",
            "tool_kind": "permission",
            "outcome": "denied",
            "metadata": {
                "target": name,
                "reason": "reserved_auto_namespace",
            },
        }
    ]


def test_non_reserved_missing_skill_does_not_emit_reserved_namespace_audit(loader, monkeypatch):
    audit_events: list[dict] = []

    class AuditSink:
        def log_tool_invocation(self, **kwargs):
            audit_events.append(kwargs)

    monkeypatch.setattr(skills_mod, "sel", lambda: AuditSink())

    assert loader.delete_skill("ordinary") is False
    assert audit_events == []


def _select_snapshot_reader(monkeypatch, reader: str) -> None:
    if reader == "pinned":
        if not skills_mod.pinned_fs.supports_pinned_tree_walk():
            pytest.skip("descriptor-pinned tree reads are unavailable")
        return
    monkeypatch.setattr(skills_mod.pinned_fs, "supports_pinned_tree_walk", lambda: False)


@pytest.mark.parametrize("reader", ["pinned", "fallback"])
def test_skill_snapshot_file_limit_accepts_boundary_and_refuses_before_read(
    loader, monkeypatch, tmp_path, reader
):
    _select_snapshot_reader(monkeypatch, reader)
    root = tmp_path / f"file-bound-{reader}"
    root.mkdir()
    leaf = root / "SKILL.md"
    boundary = "ééa".encode("utf-8")
    assert len(boundary) == 5
    leaf.write_bytes(boundary)
    platform_mode = stat.S_IMODE(leaf.stat().st_mode)
    monkeypatch.setattr(skills_mod, "_SKILL_SNAPSHOT_MAX_FILE_BYTES", len(boundary))
    monkeypatch.setattr(skills_mod, "_SKILL_SNAPSHOT_MAX_TOTAL_BYTES", len(boundary) * 2)

    snapshot = loader._skill_tree_snapshot(root)

    assert snapshot is not None
    assert snapshot.files[Path("SKILL.md")] == boundary
    assert snapshot.file_modes[Path("SKILL.md")] == platform_mode

    leaf.write_bytes(boundary + b"x")

    def unexpected_payload_read(*_args, **_kwargs):
        pytest.fail("oversized file reached payload allocation")

    monkeypatch.setattr(
        skills_mod.SkillsLoader,
        "_stable_file_payload",
        staticmethod(unexpected_payload_read),
    )
    assert loader._skill_tree_snapshot(root) is None


@pytest.mark.parametrize("reader", ["pinned", "fallback"])
def test_skill_snapshot_aggregate_limit_accepts_boundary_and_refuses_excess(
    loader, monkeypatch, tmp_path, reader
):
    _select_snapshot_reader(monkeypatch, reader)
    root = tmp_path / f"aggregate-bound-{reader}"
    root.mkdir()
    (root / "SKILL.md").write_bytes(b"12345")
    (root / ".meta.json").write_bytes(b"67890")
    monkeypatch.setattr(skills_mod, "_SKILL_SNAPSHOT_MAX_FILE_BYTES", 10)
    monkeypatch.setattr(skills_mod, "_SKILL_SNAPSHOT_MAX_TOTAL_BYTES", 10)

    snapshot = loader._skill_tree_snapshot(root)

    assert snapshot is not None
    assert sum(len(payload) for payload in snapshot.files.values()) == 10

    (root / "extra").write_bytes(b"x")
    assert loader._skill_tree_snapshot(root) is None


@pytest.mark.parametrize("reader", ["pinned", "fallback"])
def test_skill_snapshot_entry_and_depth_limits_accept_boundary_and_refuse_excess(
    loader, monkeypatch, tmp_path, reader
):
    _select_snapshot_reader(monkeypatch, reader)
    root = tmp_path / f"shape-bound-{reader}"
    root.mkdir()
    (root / "a").write_bytes(b"")
    (root / "b").write_bytes(b"")
    monkeypatch.setattr(skills_mod, "_SKILL_SNAPSHOT_MAX_ENTRIES", 2)
    monkeypatch.setattr(skills_mod, "_SKILL_SNAPSHOT_MAX_DEPTH", 2)

    assert loader._skill_tree_snapshot(root) is not None
    (root / "c").write_bytes(b"")
    assert loader._skill_tree_snapshot(root) is None

    (root / "c").unlink()
    (root / "a").unlink()
    (root / "b").unlink()
    (root / "d").mkdir()
    (root / "d" / "at-boundary").write_bytes(b"")
    monkeypatch.setattr(skills_mod, "_SKILL_SNAPSHOT_MAX_ENTRIES", 10)
    assert loader._skill_tree_snapshot(root) is not None

    (root / "d" / "too-deep").mkdir()
    (root / "d" / "too-deep" / "leaf").write_bytes(b"")
    assert loader._skill_tree_snapshot(root) is None


@pytest.mark.parametrize("reader", ["pinned", "fallback"])
def test_skill_snapshot_refuses_file_that_expands_during_read(
    loader, monkeypatch, tmp_path, reader
):
    _select_snapshot_reader(monkeypatch, reader)
    root = tmp_path / f"expanding-{reader}"
    root.mkdir()
    leaf = root / "SKILL.md"
    leaf.write_bytes(b"body")
    real_read = skills_mod.os.read
    expanded = False

    def expanding_read(fd, size):
        nonlocal expanded
        data = real_read(fd, size)
        if data and not expanded:
            with leaf.open("ab") as handle:
                handle.write(b"x")
            expanded = True
        return data

    monkeypatch.setattr(skills_mod.os, "read", expanding_read)

    assert loader._skill_tree_snapshot(root) is None
    assert expanded is True


def test_skill_snapshot_fallback_uses_held_no_reparse_handles(loader, tmp_path):
    root = tmp_path / "windows-fallback"
    nested = root / "scripts"
    nested.mkdir(parents=True)
    leaf = nested / "run.py"
    leaf.write_text("print('✓')\n", encoding="utf-8")
    expected = leaf.read_bytes()
    file_mode = stat.S_IMODE(leaf.stat().st_mode)
    dir_mode = stat.S_IMODE(nested.stat().st_mode)
    native_snapshot_reader = skills_mod.pinned_fs.supports_pinned_tree_walk
    real_pin = skills_mod.platform_compat.pin_directory
    real_open = skills_mod.platform_compat.open_file_no_reparse
    pinned: list[Path] = []
    opened: list[Path] = []

    def recording_pin(path):
        pinned.append(Path(path))
        return real_pin(path)

    def recording_open(path, *, nonblocking=False):
        opened.append(Path(path))
        return real_open(path, nonblocking=nonblocking)

    with pytest.MonkeyPatch.context() as fallback:
        fallback.setattr(skills_mod.pinned_fs, "supports_pinned_tree_walk", lambda: False)
        fallback.setattr(skills_mod.platform_compat, "pin_directory", recording_pin)
        fallback.setattr(
            skills_mod.platform_compat,
            "open_file_no_reparse",
            recording_open,
        )

        snapshot = loader._skill_tree_snapshot(root)

        assert snapshot is not None
        assert pinned == [root, nested]
        assert opened == [leaf]
        assert snapshot.files[Path("scripts/run.py")] == expected
        assert snapshot.file_modes[Path("scripts/run.py")] == file_mode
        assert snapshot.dir_modes[Path("scripts")] == dir_mode

    assert skills_mod.pinned_fs.supports_pinned_tree_walk is native_snapshot_reader
    assert loader._skill_tree_snapshot(root) is not None


def test_windows_snapshot_path_alias_uses_opened_identity(loader, tmp_path):
    root = tmp_path / "windows-path-alias"
    root.mkdir()
    skill = root / "SKILL.md"
    skill.write_bytes(b"body")
    identity_checks: list[tuple[int, str]] = []

    def same_opened_object(fd, expected):
        identity_checks.append((fd, os.fspath(expected)))
        return True

    with pytest.MonkeyPatch.context() as windows_fallback:
        windows_fallback.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
        windows_fallback.setattr(
            skills_mod.pinned_fs,
            "supports_pinned_tree_walk",
            lambda: False,
        )
        # Native Windows validates handle identity without requiring a
        # descriptor-derived path. That API can be unreadable while the exact
        # handle identity remains valid.
        windows_fallback.setattr(skills_mod.pinned_fs, "fd_real_path", lambda _fd: None)
        windows_fallback.setattr(
            skills_mod.platform_compat,
            "opened_path_identity_matches",
            same_opened_object,
        )

        snapshot = loader._skill_tree_snapshot(root)

    assert snapshot is not None
    assert snapshot.files[Path("SKILL.md")] == b"body"
    assert len(identity_checks) == 4


def test_windows_snapshot_path_identity_mismatch_refuses(loader, tmp_path, monkeypatch):
    root = tmp_path / "windows-path-mismatch"
    root.mkdir()
    (root / "SKILL.md").write_bytes(b"body")
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(skills_mod.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: False,
    )

    assert loader._skill_tree_snapshot(root) is None


def test_windows_snapshot_same_handle_ignores_unstable_crt_inode(loader, tmp_path, monkeypatch):
    root = tmp_path / "windows-unstable-crt-inode"
    root.mkdir()
    (root / "SKILL.md").write_bytes(b"body")
    real_fstat = skills_mod.os.fstat
    file_reads = 0

    def unstable_regular_inode(fd):
        nonlocal file_reads
        result = real_fstat(fd)
        if not stat.S_ISREG(result.st_mode):
            return result
        file_reads += 1
        fields = list(result)
        fields[1] = int(result.st_ino) + file_reads
        return os.stat_result(fields)

    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(skills_mod.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    monkeypatch.setattr(skills_mod.os, "fstat", unstable_regular_inode)

    snapshot = loader._skill_tree_snapshot(root)

    assert snapshot is not None
    assert snapshot.files[Path("SKILL.md")] == b"body"
    assert file_reads >= 2


def test_windows_snapshot_native_identity_overrides_stale_posix_inode_fields(
    loader, tmp_path, monkeypatch
):
    root = tmp_path / "windows-stale-inode-fields"
    root.mkdir()
    (root / "SKILL.md").write_bytes(b"body")
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(skills_mod.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    # CRT ``st_dev``/``st_ino`` values can disagree between an lstat and a
    # Windows handle even when native file IDs authenticate the same object.
    monkeypatch.setattr(skills_mod.os.path, "samestat", lambda _before, _opened: False)

    snapshot = loader._skill_tree_snapshot(root)

    assert snapshot is not None
    assert snapshot.files[Path("SKILL.md")] == b"body"


def test_windows_snapshot_hardlink_still_refuses(loader, tmp_path, monkeypatch):
    root = tmp_path / "windows-hardlink"
    root.mkdir()
    skill = root / "SKILL.md"
    skill.write_bytes(b"body")
    os.link(skill, tmp_path / "skill-alias")
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(skills_mod.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )

    assert loader._skill_tree_snapshot(root) is None


def test_windows_snapshot_scandir_zero_nlink_still_reads(loader, tmp_path, monkeypatch):
    """os.DirEntry.stat() sets st_ino/st_dev/st_nlink to 0 on Windows because the
    directory-enumeration data carries no link count. The fallback snapshot must
    not fail-close every candidate on that zeroed pre-open link count -- the
    single-link requirement is enforced on the OPENED descriptor, whose fstat
    reads the real link count on Windows. Under the pre-fix ``before.st_nlink !=
    1`` gate this returns None for every regular file and collapses the whole
    pending pipeline (empty descriptions, get_pending None, approve None)."""
    root = tmp_path / "windows-scandir-zero-nlink"
    (root / "scripts").mkdir(parents=True)
    (root / "SKILL.md").write_bytes(b"body")
    (root / "scripts" / "run.py").write_bytes(b"print(1)\n")

    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(skills_mod.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )

    real_scandir = skills_mod.os.scandir

    class _WinEntry:
        """A DirEntry whose stat() zeroes st_ino/st_dev/st_nlink like Windows."""

        def __init__(self, real):
            self._real = real
            self.name = real.name
            self.path = real.path

        def is_symlink(self):
            return self._real.is_symlink()

        def is_dir(self, *, follow_symlinks=True):
            return self._real.is_dir(follow_symlinks=follow_symlinks)

        def stat(self, *, follow_symlinks=True):
            fields = list(self._real.stat(follow_symlinks=follow_symlinks))
            fields[1] = fields[2] = fields[3] = 0  # st_ino, st_dev, st_nlink
            return os.stat_result(fields)

    def windows_scandir(path):
        inner = real_scandir(path)

        class _Scanner:
            def __enter__(self):
                inner.__enter__()
                return self

            def __exit__(self, *exc):
                return inner.__exit__(*exc)

            def __iter__(self):
                for real in inner:
                    yield _WinEntry(real)

        return _Scanner()

    monkeypatch.setattr(skills_mod.os, "scandir", windows_scandir)

    snapshot = loader._skill_tree_snapshot(root)
    assert snapshot is not None
    assert snapshot.files[Path("SKILL.md")] == b"body"
    assert snapshot.files[Path("scripts/run.py")] == b"print(1)\n"

    # The single-link requirement is preserved: a genuine hardlink still refuses
    # via the opened descriptor's real link count, even though scandir reports 0
    # for it too.
    os.link(root / "SKILL.md", tmp_path / "skill-alias")
    assert loader._skill_tree_snapshot(root) is None


def test_approval_refuses_preclaim_hardlink_candidate(loader):
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
    original = pending_skill.read_bytes()
    alias = loader._dir / "candidate-hardlink-alias"
    os.link(pending_skill, alias)
    assert pending_skill.stat().st_nlink == 2

    assert loader.approve_pending_skill(slug) is None
    assert pending_skill.read_bytes() == original
    assert alias.read_bytes() == original
    assert not (loader._dir / "auto" / slug).exists()


def test_candidate_inode_guard_fails_closed_on_open_error(loader, monkeypatch):
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
    real_open = skills_mod.platform_compat.open_file_no_reparse

    def fail_candidate_open(path, *args, **kwargs):
        if Path(path) == skill_file:
            raise PermissionError("injected candidate open failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(skills_mod.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "open_file_no_reparse",
        fail_candidate_open,
    )

    assert loader.get_pending_skill(slug) is None


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows snapshots use CreateFileW no-reparse identity, not os.open",
)
def test_skill_tree_hash_authenticates_opened_inode_without_nofollow(loader, monkeypatch):
    live_dir = _write_live(loader, "tree-hash-inode", version=1, body="SAFE")
    live_skill = live_dir / "SKILL.md"
    victim = loader._dir / "tree-hash-victim"
    victim.write_text("VICTIM", encoding="utf-8")
    real_open = skills_mod.os.open

    def open_swapped_inode(path, flags, *args, **kwargs):
        if os.fspath(path) == os.fspath(live_skill):
            return real_open(victim, flags, *args, **kwargs)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.delattr(skills_mod.os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(skills_mod.os, "open", open_swapped_inode)

    assert loader._skill_tree_hash(live_dir) is None
    assert victim.read_text(encoding="utf-8") == "VICTIM"


def test_staging_binds_validated_bytes_not_file_reread(loader):
    """A concurrent overwrite of the PUBLIC pending SKILL.md landing inside the
    staging window (after the content write, before staging completes) must not
    be vouched for: the binding hashes the in-memory validated bytes, never a
    re-read of the publicly writable file, so the unattended promotion refuses
    the swapped candidate instead of installing unvalidated prose."""
    _write_live(loader, "stage-tamper", version=1, body="OLD")
    binding: list[str] = []
    real_write_text = Path.write_text
    slug = "stage-tamper-update"

    def meta_write_then_tamper(self, data, *args, **kwargs):
        result = real_write_text(self, data, *args, **kwargs)
        if self.name == ".meta.json" and self.parent.name == slug:
            # Simulates the concurrent writer hitting the still-public candidate
            # between the SKILL.md write and any later stage-side read-back.
            real_write_text(self.parent / "SKILL.md", "## Steps\n\nINJECTED\n", encoding="utf-8")
        return result

    with pytest.MonkeyPatch.context() as staging_patch:
        staging_patch.setattr(Path, "write_text", meta_write_then_tamper)
        _stage_update(
            loader,
            slug,
            target="auto/stage-tamper",
            notify=False,
            unattended=True,
            unattended_binding_out=binding,
        )

    assert Path.write_text is real_write_text
    # The tamper IS on disk (the file is public), but the binding must not
    # vouch for it: promotion verifies claimed bytes against the binding and
    # refuses.
    assert (
        loader.auto_apply_pending_update(
            slug,
            expected_candidate_binding=binding[0],
        )
        is None
    )
    assert loader.get_auto_skill_version("auto/stage-tamper") == 1
    assert "INJECTED" not in (loader._dir / "auto" / "stage-tamper" / "SKILL.md").read_text(
        encoding="utf-8"
    )


def test_attended_update_promotes_fresh_validated_inode(loader, monkeypatch):
    """A post-validation mutation cannot change the published generation."""
    _write_live(loader, "attended-inode", version=1, body="OLD")
    _stage_update(
        loader,
        "attended-inode-update",
        target="auto/attended-inode",
        body="## Steps\n\nSAFE-ATTENDED-BODY",
    )
    pending_body = loader._pending_root() / "attended-inode-update" / "SKILL.md"
    retained_fd = None if os.name == "nt" else os.open(pending_body, os.O_WRONLY)
    real_validate = loader._validate_and_redact_candidate

    def validate_then_mutate_claim(src, name):
        result = real_validate(src, name)
        if result is not None:
            if retained_fd is None:
                (src / "SKILL.md").write_bytes(b"## Steps\r\n\r\nPATH-TAMPER\r\n")
            else:
                os.lseek(retained_fd, 0, os.SEEK_SET)
                os.ftruncate(retained_fd, 0)
                os.write(retained_fd, b"## Steps\n\nRETAINED-HANDLE-TAMPER\n")
        return result

    monkeypatch.setattr(loader, "_validate_and_redact_candidate", validate_then_mutate_claim)
    try:
        assert loader.approve_pending_update("attended-inode-update") == "auto/attended-inode"
    finally:
        if retained_fd is not None:
            os.close(retained_fd)

    live = (loader._dir / "auto" / "attended-inode" / "SKILL.md").read_text(encoding="utf-8")
    assert "SAFE-ATTENDED-BODY" in live
    assert "RETAINED-HANDLE-TAMPER" not in live
    assert "PATH-TAMPER" not in live


# ── whole-publication invariant regressions ──


def test_live_drift_at_generation_swap_is_restored_without_overwrite(loader, monkeypatch):
    """A live edit landing at the mutation boundary wins over the candidate."""
    live_dir = _write_live(loader, "swap-drift", version=1, body="ORIGINAL")
    live_file = live_dir / "SKILL.md"
    slug = "swap-drift-update"
    _stage_update(loader, slug, target="auto/swap-drift", base_version=1)
    edited = live_file.read_text(encoding="utf-8").replace("ORIGINAL", "CONCURRENT EDIT")
    real_rename = skills_mod.platform_compat.rename_no_replace
    injected = False

    def edit_before_capture(source, destination):
        nonlocal injected
        if Path(source) == live_dir and Path(destination).name.startswith(".previous-"):
            assert injected is False
            injected = True
            live_file.write_text(edited, encoding="utf-8")
        return real_rename(source, destination)

    monkeypatch.setattr(skills_mod.platform_compat, "rename_no_replace", edit_before_capture)

    assert loader.approve_pending_update(slug) is None
    assert injected is True
    assert live_file.read_text(encoding="utf-8") == edited
    assert not (live_dir / ".versions").exists()
    assert (loader._pending_root() / slug / "SKILL.md").is_file()


def test_marker_failure_preserves_prepared_journal_for_consumption(loader, monkeypatch):
    """A failed marker write must not clobber the recoverable prepared journal."""
    _write_live(loader, "marker-fallback", version=1, body="OLD")
    slug = "marker-fallback-update"
    _stage_update(loader, slug, target="auto/marker-fallback", base_version=1)
    commit_called = False

    def unexpected_commit(*_args, **_kwargs):
        nonlocal commit_called
        commit_called = True
        return False

    monkeypatch.setattr(loader, "_write_completion_marker", lambda _claim: False)
    monkeypatch.setattr(loader, "_commit_claim_lock_state", unexpected_commit)

    assert loader.approve_pending_update(slug) == "auto/marker-fallback"
    assert commit_called is False
    claims = list(loader._claims_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    claim = claims[0]
    lock_path = loader._claim_lock_path(claim.name)
    fd = _open_lock_for_test(loader, lock_path)
    acquired = skills_mod.platform_compat.try_acquire_lock(fd, exclusive=True)
    try:
        assert acquired is True
        assert loader._authenticated_claim_publication(fd, lock_path, claim.name) is not None
    finally:
        if acquired:
            skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert restarted.get_auto_skill_version("auto/marker-fallback") == 2
    assert not claim.exists()


def test_completion_state_failure_uses_authenticated_marker(loader, monkeypatch):
    """A committed marker makes a failed completion-state write recoverable."""
    _write_live(loader, "journal-fallback", version=1, body="OLD")
    slug = "journal-fallback-update"
    _stage_update(loader, slug, target="auto/journal-fallback", base_version=1)
    monkeypatch.setattr(loader, "_commit_claim_lock_state", lambda *_args: False)

    assert loader.approve_pending_update(slug) == "auto/journal-fallback"
    claims = list(loader._claims_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    assert loader._authenticated_completion_marker(claims[0]) is True

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert restarted.get_auto_skill_version("auto/journal-fallback") == 2
    assert not claims[0].exists()


def test_string_false_has_scripts_is_not_a_boolean_true(loader, snapshot_reader):
    """Untrusted JSON strings never opt a candidate into script handling."""
    _write_live(loader, "strict-bool", version=1, body="OLD")
    binding: list[str] = []
    slug = "strict-bool-update"
    _stage_update(
        loader,
        slug,
        target="auto/strict-bool",
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )
    metadata = loader._pending_root() / slug / ".meta.json"
    raw = json.loads(metadata.read_text(encoding="utf-8"))
    raw["has_scripts"] = "false"
    metadata.write_text(json.dumps(raw), encoding="utf-8")

    pending = next(row for row in loader.list_pending_skills() if row["slug"] == slug)
    assert pending["has_scripts"] is False
    assert loader.auto_apply_pending_update(
        slug,
        expected_candidate_binding=binding[0],
    ) == ("auto/strict-bool", 2)


def test_sync_skill_tree_uses_authenticated_writable_handle_on_windows(
    loader, monkeypatch, tmp_path
):
    """Windows durability uses O_RDWR without dropping no-follow/inode checks."""
    root = tmp_path / "windows-sync"
    root.mkdir()
    leaf = root / "SKILL.md"
    leaf.write_text("body", encoding="utf-8")
    real_open = skills_mod.os.open
    opened_flags: list[int] = []

    def recording_open(path, flags, *args, **kwargs):
        if Path(path) == leaf:
            opened_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    monkeypatch.delattr(os, "O_ACCMODE", raising=False)
    monkeypatch.setattr(skills_mod.os, "open", recording_open)

    loader._sync_skill_tree(root)

    assert len(opened_flags) == 1
    access_mode_mask = getattr(os, "O_ACCMODE", os.O_WRONLY | os.O_RDWR)
    assert opened_flags[0] & access_mode_mask == os.O_RDWR
    if getattr(os, "O_NOFOLLOW", 0):
        assert opened_flags[0] & os.O_NOFOLLOW


def test_sync_skill_tree_refuses_inode_substitution(loader, monkeypatch, tmp_path):
    """The Windows-compatible writable open still authenticates the staged inode."""
    root = tmp_path / "inode-sync"
    root.mkdir()
    leaf = root / "SKILL.md"
    leaf.write_text("STAGED", encoding="utf-8")
    victim = tmp_path / "victim"
    victim.write_text("DO NOT TOUCH", encoding="utf-8")
    real_open = skills_mod.os.open

    def substituted_open(path, flags, *args, **kwargs):
        if Path(path) == leaf:
            return real_open(victim, flags, *args, **kwargs)
        return real_open(path, flags, *args, **kwargs)

    def native_identity_matches(fd, path):
        return os.path.samestat(os.fstat(fd), os.stat(path, follow_symlinks=False))

    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        native_identity_matches,
    )
    monkeypatch.setattr(skills_mod.os, "open", substituted_open)

    with pytest.raises(OSError, match="changed during durable open"):
        loader._sync_skill_tree(root)
    assert victim.read_text(encoding="utf-8") == "DO NOT TOUCH"
