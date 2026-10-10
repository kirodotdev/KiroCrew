"""Tests: pending skill UPDATES + per-skill version history.

Covers ``stage_skill_candidate(kind="update", ...)``, ``get_auto_skill_version``,
``read_auto_skill_body`` and ``approve_pending_update`` — the update-approval
flow that snapshots the current live version into ``auto/<slug>/.versions/``
before overwriting, and the guarantee that ``.versions`` never surfaces as a
loadable skill.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import logging
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
from kiro_crew.skill_runtime import authoring as skill_authoring
from kiro_crew.skill_runtime import auto_skills as skill_auto
from kiro_crew.skills import (
    MAX_SKILL_VERSIONS,
    AutoSkillProvenance,
    ClaimRefusal,
    SkillsLoader,
    canonical_skill_text_hash,
)

#: Lost-run bound for a wait a test's own thread must release (testing
#: conventions D8): generous, because it only matters when the release never comes.
_THREAD_WAIT_CEILING_SECS = 30.0

_HOST_WINDOWS = os.name == "nt"
_REAL_AUTHORITY_SANDBOX_REFUSAL = skills_mod._auto_skill_authority_sandbox_refusal
#: Captured at import, before any fixture patches it: whether this host really has
#: the descriptor-relative primitives. A test that switches modes mid-flow must not
#: switch ON a capability the host lacks (Windows has no ``dir_fd`` at all).
_HOST_DIR_FD_SUPPORTED = skills_mod._DIR_FD_SUPPORTED

#: The named fields ``os.stat_result`` keeps in its sequence. Every other field
#: (``st_blksize``, the float and ns times, Windows' ``st_file_attributes`` and
#: ``st_reparse_tag``) lives only in its keyword part.
_STAT_SEQUENCE_FIELDS = ("st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size")


def _restat(result: os.stat_result, **changes: int) -> os.stat_result:
    """Return *result* with some sequence fields changed and every other field kept.

    ``os.stat_result(list(result))`` rebuilds only the ten sequence fields and
    leaves the rest ``None``. A product branch that reads one of those (Windows
    ``pin_directory`` tests ``st_file_attributes``) then sees a value no real
    ``fstat`` returns and fails on ``None & int``, for a reason the test never meant.
    """
    fields = list(result)
    for name, value in changes.items():
        fields[_STAT_SEQUENCE_FIELDS.index(name)] = value
    extras = {
        name: getattr(result, name)
        for name in dir(result)
        if name.startswith("st_") and name not in _STAT_SEQUENCE_FIELDS
    }
    return os.stat_result(fields, extras)


def _different_identity(identity):
    """A same-kind identity naming a DIFFERENT object, for a mismatch fixture.

    Stays in the identity's own encoding: a POSIX ``(device, inode)`` pair moves
    its inode and a Windows 128-bit file id flips one bit. Rebuilding every kind
    as ``posix-dev-ino`` hands ``int()`` the raw bytes of a native file id.
    """
    if isinstance(identity.object_id, bytes):
        object_id = bytes([identity.object_id[0] ^ 0x01]) + identity.object_id[1:]
    else:
        object_id = identity.object_id + 1
    return skills_mod._TaggedFileIdentity(identity.kind, identity.volume, object_id)


def _pinned_directory_renames() -> bool:
    """PROBE, never a platform guess: may a directory held by ``pin_directory`` be renamed?

    POSIX allows it, so swapping a pinned directory is a real race there and the
    product's post-pin identity recheck is what refuses it. Windows refuses the
    rename itself: ``pin_directory`` opens without ``FILE_SHARE_DELETE``, so an
    injected ``os.rename`` fails with a sharing violation (WinError 32) before the
    product could observe any swap.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        held = os.path.join(tmp, "held")
        os.mkdir(held)
        fd = skills_mod.platform_compat.pin_directory(held)
        try:
            os.rename(held, f"{held}-moved")
        except OSError:
            return False
        finally:
            os.close(fd)
        return True


requires_renamable_pinned_directory = pytest.mark.skipif(
    not _pinned_directory_renames(),
    reason=(
        "injects a rename of a directory the product holds pinned; this host refuses "
        "that rename itself (a Windows pin omits FILE_SHARE_DELETE), so the swap "
        "cannot happen, and production never creates the authority on Windows"
    ),
)

# This module owns startup-certificate, legacy, provenance, and retargeting
# boundaries. Every positive path initializes explicitly through ``loader``;
# every negative ``uninitialized_loader`` path must see production fail closed.
pytestmark = pytest.mark.usefixtures("no_auto_skill_authority_startup")


@pytest.fixture()
def loader():
    loader = SkillsLoader(install_builtins=False)
    skills_mod.initialize_auto_skill_private_authority(
        skills_root=loader._dir,
        data_home=loader._private_root().parents[1],
    )
    return loader


@pytest.fixture()
def uninitialized_loader():
    return SkillsLoader(install_builtins=False)


@pytest.fixture(autouse=True)
def _simulated_windows_opened_identity(_floor_monkeypatch):
    from kiro_crew import sandbox

    with skills_mod._AUTHORITY_HOME_IDENTITIES_LOCK:
        skills_mod._AUTHORITY_HOME_IDENTITIES.clear()
    _floor_monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    _floor_monkeypatch.setattr(sandbox, "_macos_sandbox_state", lambda: None)
    real_identity = skills_mod.platform_compat.opened_file_identity
    real_fstat = os.fstat

    def opened_identity(fd):
        if skills_mod.platform_compat.IS_WINDOWS and not _HOST_WINDOWS:
            opened = real_fstat(fd)
            payload = hashlib.sha256(f"{opened.st_dev}:{opened.st_ino}".encode("ascii")).digest()[
                :16
            ]
            return opened.st_dev, b"F128" + payload
        return real_identity(fd)

    _floor_monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_file_identity",
        opened_identity,
    )
    _floor_monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        lambda: None,
    )
    _floor_monkeypatch.setattr(
        skills_mod,
        "_agent_sandbox_is_delegated",
        lambda: False,
    )


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


def _tagged_identity_for_path(loader, path):
    with loader._pin_skill_parent(path) as parent:
        return parent.native_identity


def _simulate_windows_native_handles(monkeypatch):
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "restrict_dir_to_owner",
        lambda _path: None,
    )

    def open_entry(path):
        # OPEN_REPARSE_POINT: open the entry ITSELF, never what a link names. Linux
        # spells that O_PATH | O_NOFOLLOW. macOS has no O_PATH, and O_NOFOLLOW alone
        # turns a link into ELOOP there, so it spells it O_SYMLINK instead.
        if hasattr(os, "O_PATH"):
            flags = os.O_PATH | getattr(os, "O_NOFOLLOW", 0)
        else:
            flags = os.O_RDONLY | getattr(os, "O_SYMLINK", getattr(os, "O_NOFOLLOW", 0))
        return os.open(path, flags)

    monkeypatch.setattr(skills_mod.platform_compat, "open_path_no_reparse", open_entry)


def _initialize_test_authority(loader):
    return skills_mod.initialize_auto_skill_private_authority(
        skills_root=loader._dir,
        data_home=loader._private_root().parents[1],
    )


def _authority_path(home: Path) -> Path:
    return (
        home / skills_mod._AUTHORITY_PROVENANCE_PARENT / skills_mod.AUTO_SKILL_PRIVATE_STATE_DIRNAME
    )


def test_simulated_windows_opened_identity_pins_delegation_false():
    assert skills_mod._agent_sandbox_is_delegated() is False


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


def test_refine_refuses_a_live_file_that_does_not_vet(loader, monkeypatch):
    """A refused rewrite read is a refusal, not an exception.

    ``update_auto_skill`` documents a bool, and its consolidation caller audits a
    ``False`` as ``rejected``/``update_failed``. Raising instead would skip that
    audit; answering "no metadata" would rewrite the skill without its version.
    """
    live_skill = _write_live(loader, "refine-refused", version=3, body="OLD") / "SKILL.md"
    before = live_skill.read_bytes()
    real_vet = loader._vet_unconfined_path
    monkeypatch.setattr(
        loader, "_vet_unconfined_path", lambda path: path != live_skill and real_vet(path)
    )
    loader._fm_cache.clear()

    refined = loader.update_auto_skill(
        "auto/refine-refused",
        description="refined desc",
        triggers="t",
        procedure_md="## Steps\n\nrefined",
        provenance=_prov(),
    )
    assert refined is False
    assert live_skill.read_bytes() == before


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
    assert not list(loader._live_quarantine_root().iterdir())


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


def test_a_refused_live_read_refuses_the_approval_with_the_candidate_unredacted(
    loader, monkeypatch
):
    """A live skill whose rewrite read is refused refuses the approval cleanly.

    The live version and frontmatter are rewrite reads, so a live path that does
    not vet raises instead of answering "no metadata". That must surface as
    ``PendingApprovalRefused`` (not a bare ``PermissionError`` the dashboard turns
    into a 500), and the candidate must stay byte-identical, which it can only do
    if those reads run before the in-place redaction. The preview keeps its
    ``None`` contract on the same refusal.
    """
    secret = "AKIAIOSFODNN7EXAMPLE"
    live = _write_live(loader, "refused", version=1, body="ORIGINAL")
    _stage_update(
        loader, "refused-a", target="auto/refused", body=f"## Steps\n\nuse key {secret} here"
    )
    candidate = loader._pending_root() / "refused-a" / "SKILL.md"
    before = candidate.read_bytes()
    assert secret.encode() in before
    live_skill = live / "SKILL.md"
    live_before = live_skill.read_bytes()

    real_vet = loader._vet_unconfined_path
    monkeypatch.setattr(
        loader, "_vet_unconfined_path", lambda path: path != live_skill and real_vet(path)
    )
    loader._fm_cache.clear()

    assert loader.preview_pending_update("refused-a") is None
    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        loader.approve_pending_update_checked("refused-a")
    assert refused.value.reason == "promotion_failed"
    assert candidate.read_bytes() == before
    assert live_skill.read_bytes() == live_before


# ── unattended update promotion / atomic claim ──


def test_claim_requires_resolved_private_root_to_be_sensitive(loader, monkeypatch, tmp_path):
    _write_live(loader, "alias-guard", version=1, body="OLD")
    _stage_update(loader, "alias-guard-update", target="auto/alias-guard")
    private_root = loader._private_root()
    provenance = loader._authority_provenance_path()
    physical_alias = tmp_path / "project" / "crew-data" / "skills" / "auto" / ".private"
    real_resolve = Path.resolve

    def resolve_private_root(path, *args, **kwargs):
        if path == private_root:
            return physical_alias
        return real_resolve(path, *args, **kwargs)

    checked: list[str] = []

    def sensitive(path: str) -> bool:
        checked.append(path)
        return path in {str(private_root), str(provenance)}

    monkeypatch.setattr(Path, "resolve", resolve_private_root)
    monkeypatch.setattr(skills_mod, "is_sensitive_path", sensitive)

    assert loader._claim_pending_update("alias-guard-update") is None
    assert str(private_root) in checked
    assert str(physical_alias) in checked
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

    The loader remains verify-only. Gateway startup provisions the authority
    beneath the already-precreated ``tag-grants`` mask, so construction cannot
    create either the stable parent or any claim/lock descendant.
    """
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    fresh = SkillsLoader(skills_path=skills_root, install_builtins=False)

    assert not os.path.lexists(fresh._private_root())
    assert fresh._private_state_roots_safe(create=True) is False
    skills_mod.initialize_auto_skill_private_authority(
        skills_root=fresh._dir,
        data_home=fresh._private_root().parents[1],
    )
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


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX permits a writable descriptor to survive the live-tree rename",
)
def test_auto_apply_retains_post_revalidation_descriptor_write_as_evidence(
    loader,
    monkeypatch,
):
    target = "post-publication-writer"
    live_dir = _write_live(loader, target, version=1, body="OLD")
    retained_fd = os.open(live_dir / "SKILL.md", os.O_WRONLY)
    first_binding: list[str] = []
    _stage_update(
        loader,
        f"{target}-update",
        target=f"auto/{target}",
        notify=False,
        unattended=True,
        unattended_binding_out=first_binding,
    )
    real_hash = loader._skill_tree_hash_child
    backup_hashes = 0
    edit_landed = False
    edited_generation = b"POST-PUBLICATION-EDIT\n"

    def hash_then_write_through_retained_descriptor(parent, name):
        nonlocal backup_hashes, edit_landed
        result = real_hash(parent, name)
        if parent.path == loader._live_quarantine_root():
            backup_hashes += 1
            if backup_hashes == 2:
                # The second backup hash is the final revalidation in the
                # publication function. The write lands after that snapshot has
                # returned but before completed-claim cleanup can run.
                os.lseek(retained_fd, 0, os.SEEK_SET)
                os.ftruncate(retained_fd, 0)
                os.write(retained_fd, edited_generation)
                os.fsync(retained_fd)
                edit_landed = True
        return result

    monkeypatch.setattr(
        loader,
        "_skill_tree_hash_child",
        hash_then_write_through_retained_descriptor,
    )
    try:
        assert loader.auto_apply_pending_update(
            f"{target}-update",
            expected_candidate_binding=first_binding[0],
        ) == (f"auto/{target}", 2)
    finally:
        os.close(retained_fd)

    assert edit_landed is True
    assert backup_hashes == 2
    assert loader.get_auto_skill_version(f"auto/{target}") == 2
    evidence = sorted(loader._evidence_root().glob(f"{target}-update--*"))
    assert len(evidence) == 1
    _stage, retained_tree = loader._publication_paths(evidence[0].name)
    retained = retained_tree / "SKILL.md"
    assert retained.read_bytes() == edited_generation
    assert not loader._claims_root().exists() or not list(loader._claims_root().iterdir())
    assert list(loader._live_quarantine_root().iterdir()) == [retained_tree]
    lock_path = loader._claim_lock_path(evidence[0].name)
    evidence_fd = _open_lock_for_test(loader, lock_path)
    try:
        journal = loader._authenticated_claim_evidence_state(
            evidence_fd,
            lock_path,
            evidence[0].name,
        )
        assert journal is not None
        assert journal["kind"] == "update"
        assert journal["target"] == target
    finally:
        os.close(evidence_fd)

    second_binding: list[str] = []
    _stage_update(
        loader,
        f"{target}-second-update",
        target=f"auto/{target}",
        base_version=2,
        notify=False,
        unattended=True,
        unattended_binding_out=second_binding,
    )
    assert loader.auto_apply_pending_update(
        f"{target}-second-update",
        expected_candidate_binding=second_binding[0],
    ) == (f"auto/{target}", 3)
    assert loader.get_auto_skill_version(f"auto/{target}") == 3
    assert retained.read_bytes() == edited_generation
    assert len(list(loader._evidence_root().glob("*--*"))) == 2
    assert len(list(loader._live_quarantine_root().iterdir())) == 2
    assert not loader._claims_root().exists() or not list(loader._claims_root().iterdir())


@pytest.mark.skipif(
    os.name == "nt" or os.open not in os.supports_dir_fd,
    reason="retained directory traversal requires POSIX openat",
)
def test_retained_old_live_directory_stays_in_public_ancestry(loader):
    target = "retained-live-directory"
    live = _write_live(loader, target, version=1, body="OLD")
    retained_fd = os.open(live, os.O_RDONLY | os.O_DIRECTORY)
    original_identity = os.fstat(retained_fd).st_ino
    slug = f"{target}-update"
    _stage_update(loader, slug, target=f"auto/{target}")
    try:
        assert loader.approve_pending_update(slug) == f"auto/{target}"
        parent_fd = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=retained_fd)
        try:
            assert os.path.samestat(
                os.fstat(parent_fd),
                os.stat(loader._live_quarantine_root()),
            )
            assert not os.path.samestat(
                os.fstat(parent_fd),
                os.stat(loader._private_root()),
            )
        finally:
            os.close(parent_fd)
    finally:
        os.close(retained_fd)

    public_old_live = list(loader._live_quarantine_root().iterdir())
    assert len(public_old_live) == 1
    assert public_old_live[0].stat().st_ino == original_identity
    private_directory_inodes = {
        path.stat().st_ino
        for path in loader._private_root().rglob("*")
        if path.is_dir() and not path.is_symlink()
    }
    assert original_identity not in private_directory_inodes


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
    """A by-name write to the trusted private materialization before binding
    verification must be refused because the binding sees changed private bytes."""
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
    """A by-name private-materialization write after redaction cannot reach live."""
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
    """A by-name scripts mutation after the probe cannot enter the live tree."""
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
        # in the promotion): a scripts directory planted by name in the trusted
        # materialization must never be copied into the live tree.
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
    real_write = skill_authoring.update_skill
    real_lock = loader._promotion_lock

    def blocked_dashboard_write(owner, name, content):
        writer_has_lock.set()
        assert allow_writer.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        return real_write(owner, name, content)

    @contextlib.contextmanager
    def observed_lock(slug):
        if threading.current_thread().name.startswith("auto-apply"):
            auto_lock_attempted.set()
        with real_lock(slug) as acquired:
            yield acquired

    monkeypatch.setattr(skill_authoring, "update_skill", blocked_dashboard_write)
    monkeypatch.setattr(loader, "_promotion_lock", observed_lock)

    with (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="dashboard-edit") as edit_pool,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="auto-apply") as apply_pool,
    ):
        edit_future = edit_pool.submit(
            loader.update_skill, "auto/dashboard-race", dashboard_content
        )
        assert writer_has_lock.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        try:
            apply_future = apply_pool.submit(
                loader.auto_apply_pending_update,
                "dashboard-race-update",
                expected_candidate_binding=binding[0],
            )
            assert auto_lock_attempted.wait(timeout=_THREAD_WAIT_CEILING_SECS)
            assert not apply_future.done()
        finally:
            allow_writer.set()
        assert edit_future.result(timeout=_THREAD_WAIT_CEILING_SECS) is True
        assert apply_future.result(timeout=_THREAD_WAIT_CEILING_SECS) is None

    assert live_file.read_text(encoding="utf-8") == dashboard_content
    assert (loader._pending_root() / "dashboard-race-update" / "SKILL.md").exists()
    assert not (live_dir / ".versions").exists()


@pytest.mark.parametrize(
    ("method_name", "owner", "helper_name", "extra_args"),
    [
        # Each mutation's first write, in the module that performs it. The
        # facade takes the target lock, then calls its owner; ``delete_skill``
        # stays in the facade, so its recursive removal is the observed write.
        ("update_skill", skill_authoring, "update_skill", ("dashboard body",)),
        ("delete_skill", shutil, "rmtree", ()),
        ("set_pinned", skill_authoring, "set_pinned", (True,)),
        ("set_inject_on_trigger", skill_authoring, "set_inject_on_trigger", (False,)),
        ("archive_auto_skill", skill_auto, "archive_auto_skill", ()),
    ],
)
def test_live_auto_mutators_share_promotion_lock(
    loader, monkeypatch, method_name, owner, helper_name, extra_args
):
    _write_live(loader, "mutation-lock", version=1, body="OLD")
    name = "auto/mutation-lock"
    attempted = threading.Event()
    mutation_entered = threading.Event()
    real_lock = loader._promotion_lock
    real_mutation = getattr(owner, helper_name)

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
    monkeypatch.setattr(owner, helper_name, observed_mutation)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-mutator") as pool:
        with real_lock("mutation-lock") as acquired:
            assert acquired is True
            future = pool.submit(getattr(loader, method_name), name, *extra_args)
            assert attempted.wait(timeout=_THREAD_WAIT_CEILING_SECS)
            assert not mutation_entered.is_set()
        assert future.result(timeout=_THREAD_WAIT_CEILING_SECS) is True
    assert mutation_entered.is_set()


@pytest.mark.parametrize("name", ["AUTO/mutation-lock", "Auto/mutation-lock", "aUtO/mutation-lock"])
def test_live_auto_mutator_refuses_noncanonical_namespace_alias(loader, monkeypatch, name):
    def unexpected_write(_owner, _name, _content):
        pytest.fail("noncanonical auto namespace reached the unlocked writer")

    monkeypatch.setattr(skill_authoring, "update_skill", unexpected_write)

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
    reason="Windows non-delete-sharing parent handles prohibit the transient replacement",
)
def test_claim_capture_ignores_transient_pending_path_replacement(loader, monkeypatch, tmp_path):
    """Capture and quarantine must describe the same pending child inode."""
    target = "transient-pending-capture"
    slug = f"{target}-update"
    live = _write_live(loader, target, version=1, body="OLD")
    _stage_update(loader, slug, target=f"auto/{target}", body="## Steps\n\nREVIEWED")
    pending_root = loader._pending_root()
    detached = pending_root.with_name(".pending-capture-detached")
    replacement = tmp_path / "replacement-pending"
    replacement_candidate = replacement / slug
    replacement_candidate.mkdir(parents=True)
    (replacement_candidate / "SKILL.md").write_text(
        "---\nname: auto/replacement\n---\n\n## Steps\n\nUNREVIEWED\n",
        encoding="utf-8",
    )
    (replacement_candidate / ".meta.json").write_text(
        json.dumps(
            {
                "kind": "update",
                "target": f"auto/{target}",
                "base_version": 1,
            }
        ),
        encoding="utf-8",
    )
    preserved_replacement = tmp_path / "replacement-preserved"
    real_snapshot_child = loader._skill_tree_snapshot_child
    swapped = False

    def capture_while_public_path_is_replaced(parent, name):
        nonlocal swapped
        if parent.path == pending_root and name == slug and not swapped:
            pending_root.rename(detached)
            replacement.rename(pending_root)
            swapped = True
            try:
                snapshot = loader._skill_tree_snapshot(pending_root / name)
                assert snapshot is not None
            finally:
                pending_root.rename(preserved_replacement)
                detached.rename(pending_root)
            return snapshot
        return real_snapshot_child(parent, name)

    monkeypatch.setattr(
        loader,
        "_skill_tree_snapshot_child",
        capture_while_public_path_is_replaced,
    )

    assert loader.approve_pending_update(slug) is None
    assert swapped is True
    live_body = (live / "SKILL.md").read_text(encoding="utf-8")
    assert "OLD" in live_body
    assert "REVIEWED" in (pending_root / slug / "SKILL.md").read_text(encoding="utf-8")
    assert "UNREVIEWED" in (preserved_replacement / slug / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.skipif(
    os.name == "nt",
    reason="simulated Windows control-flow test requires POSIX child rename semantics",
)
def test_simulated_windows_claim_refuses_child_swap_before_pin(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    """The Windows branch must bind admission to the child it opens."""
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    _initialize_test_authority(loader)
    target = "windows-child-swap"
    slug = f"{target}-update"
    live = _write_live(loader, target, version=1, body="OLD-WINDOWS-LIVE")
    _stage_update(
        loader,
        slug,
        target=f"auto/{target}",
        body="## Steps\n\nREVIEWED-WINDOWS",
    )
    pending_root = loader._pending_root()
    candidate = pending_root / slug
    preserved_original = pending_root / f".{slug}-original"
    prepared_replacement = pending_root / f".{slug}-replacement"
    shutil.copytree(candidate, prepared_replacement)
    replacement_skill = prepared_replacement / "SKILL.md"
    replacement_skill.write_text(
        replacement_skill.read_text(encoding="utf-8").replace(
            "REVIEWED-WINDOWS",
            "UNREVIEWED-WINDOWS",
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    real_pin_child = loader._pin_skill_child_parent
    swapped = False

    @contextlib.contextmanager
    def swap_candidate_before_pin(parent, name, *, create, created_out=None):
        nonlocal swapped
        if parent.path == pending_root and name == slug and not swapped:
            candidate.rename(preserved_original)
            prepared_replacement.rename(candidate)
            swapped = True
        with real_pin_child(
            parent,
            name,
            create=create,
            created_out=created_out,
        ) as child:
            yield child

    monkeypatch.setattr(loader, "_pin_skill_child_parent", swap_candidate_before_pin)

    assert loader.approve_pending_update(slug) is None
    assert swapped is True
    assert "OLD-WINDOWS-LIVE" in (live / "SKILL.md").read_text(encoding="utf-8")
    assert "REVIEWED-WINDOWS" in (preserved_original / "SKILL.md").read_text(encoding="utf-8")
    assert "UNREVIEWED-WINDOWS" in (candidate / "SKILL.md").read_text(encoding="utf-8")
    trusted_skill_bodies = [
        path.read_text(encoding="utf-8") for path in loader._private_root().rglob("SKILL.md")
    ]
    assert all("UNREVIEWED-WINDOWS" not in body for body in trusted_skill_bodies)
    assert not list(loader._quarantine_root().glob(f"{slug}--*"))


@pytest.mark.skipif(
    os.name == "nt",
    reason="simulated Windows control-flow test uses POSIX descriptor stand-ins",
)
def test_simulated_windows_claim_accepts_unchanged_candidate(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    """The added Windows admission binding must preserve ordinary promotion."""
    native_checks: list[Path] = []
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)

    def same_native_object(_fd, path):
        native_checks.append(Path(path))
        return True

    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        same_native_object,
    )
    _initialize_test_authority(loader)
    target = "windows-unchanged-claim"
    slug = f"{target}-update"
    live = _write_live(loader, target, version=1, body="OLD-WINDOWS-LIVE")
    _stage_update(
        loader,
        slug,
        target=f"auto/{target}",
        body="## Steps\n\nSAFE-WINDOWS-UPDATE",
    )

    assert loader.approve_pending_update(slug) == f"auto/{target}"
    live_body = (live / "SKILL.md").read_text(encoding="utf-8")
    assert "SAFE-WINDOWS-UPDATE" in live_body
    assert "OLD-WINDOWS-LIVE" not in live_body
    assert native_checks


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows non-delete-sharing file handles block the claim rename",
)
def test_claim_snapshot_boundary_separates_retained_writer_from_private_claim(
    loader,
    monkeypatch,
):
    """A retained public descriptor cannot change the trusted materialization."""
    target = "claim-boundary-after"
    live = _write_live(loader, target, version=1, body="OLD")
    slug = f"{target}-update"
    _stage_update(
        loader,
        slug,
        target=f"auto/{target}",
        body="## Steps\n\nSAFE-BEFORE-RENAME",
    )
    pending = loader._pending_root() / slug
    retained_dir_fd = os.open(pending, os.O_RDONLY | os.O_DIRECTORY)
    retained_file_fd = os.open(pending / "SKILL.md", os.O_WRONLY)
    real_rename = loader._rename_skill_child_no_replace
    mutated = False

    def rename_then_write(source, source_name, destination, destination_name, **kwargs):
        nonlocal mutated
        result = real_rename(source, source_name, destination, destination_name, **kwargs)
        if source_name == slug and destination.path == loader._quarantine_root():
            os.lseek(retained_file_fd, 0, os.SEEK_SET)
            os.ftruncate(retained_file_fd, 0)
            os.write(retained_file_fd, b"## Steps\n\nRETAINED-WRITE\n")
            os.fsync(retained_file_fd)
            mutated = True
        return result

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", rename_then_write)
    try:
        assert loader.approve_pending_update(slug) == f"auto/{target}"
        auto_fd = os.open("../..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=retained_dir_fd)
        try:
            assert os.path.samestat(os.fstat(auto_fd), os.stat(loader._dir / "auto"))
            assert not os.path.samestat(os.fstat(auto_fd), os.stat(loader._private_root()))
        finally:
            os.close(auto_fd)
    finally:
        os.close(retained_file_fd)
        os.close(retained_dir_fd)

    assert mutated is True
    assert "SAFE-BEFORE-RENAME" in (live / "SKILL.md").read_text(encoding="utf-8")
    assert "RETAINED-WRITE" not in (live / "SKILL.md").read_text(encoding="utf-8")
    quarantine = list(loader._quarantine_root().glob(f"{slug}--*"))
    evidence = list(loader._evidence_root().glob(f"{slug}--*"))
    assert len(quarantine) == 1
    assert len(evidence) == 1
    assert (quarantine[0] / "SKILL.md").read_bytes() == b"## Steps\n\nRETAINED-WRITE\n"
    assert "SAFE-BEFORE-RENAME" in (evidence[0] / "SKILL.md").read_text(encoding="utf-8")
    assert skills_mod.is_sensitive_path(str(loader._private_root())) is True


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
        assert loader.approve_pending_update(slug) == "auto/retained-first"
    finally:
        os.close(retained_fd)

    assert "SAFE" in (first / "SKILL.md").read_text(encoding="utf-8")
    assert "SECOND" in (second / "SKILL.md").read_text(encoding="utf-8")
    quarantine = list(loader._quarantine_root().glob(f"{slug}--*"))
    evidence = list(loader._evidence_root().glob(f"{slug}--*"))
    assert len(quarantine) == 1
    assert len(evidence) == 1
    assert (
        json.loads((quarantine[0] / ".meta.json").read_text(encoding="utf-8"))["target"]
        == "auto/retained-second"
    )
    assert (
        json.loads((evidence[0] / ".meta.json").read_text(encoding="utf-8"))["target"]
        == "auto/retained-first"
    )


def test_claim_rejects_windows_style_metadata_replacement_after_rename(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    """The by-name platform branch enforces the same complete-generation digest."""
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    _initialize_test_authority(loader)
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
        barrier.wait(timeout=_THREAD_WAIT_CEILING_SECS)
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
    evidence = list(loader._evidence_root().glob("authoritative-update--*"))
    assert len(evidence) == 1
    _stage, retained = loader._publication_paths(evidence[0].name)
    assert (retained / "SKILL.md").is_file()
    assert loader._claim_lock_path(evidence[0].name).is_file()
    assert not loader._claims_root().exists() or not list(loader._claims_root().iterdir())


def test_auto_apply_reports_committed_update_when_consumption_needs_recovery(
    loader,
    monkeypatch,
):
    target = "committed-recovery"
    live = _write_live(loader, target, version=1, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        f"{target}-update",
        target=f"auto/{target}",
        body="## Steps\n\nNEW",
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )
    seen: list[dict] = []
    skills_mod.set_update_auto_applied_hook(seen.append)
    monkeypatch.setattr(loader, "_commit_claim_consumption", lambda *_args: False)
    recovery_pending: list[tuple[str, int]] = []
    try:
        assert (
            loader.auto_apply_pending_update(
                f"{target}-update",
                expected_candidate_binding=binding[0],
                recovery_pending_out=recovery_pending,
            )
            is None
        )
    finally:
        skills_mod.set_update_auto_applied_hook(None)

    assert recovery_pending == [(f"auto/{target}", 2)]
    assert "NEW" in (live / "SKILL.md").read_text(encoding="utf-8")
    assert seen == [
        {
            "name": f"auto/{target}",
            "slug": f"{target}-update",
            "target": f"auto/{target}",
            "new_version": 2,
            "description": "",
            "recovery_pending": True,
        }
    ]
    assert not (loader._pending_root() / f"{target}-update").exists()
    assert list(loader._claims_root().glob(f"{target}-update--*"))


def test_recovery_pending_target_stays_lifecycle_exempt_until_claim_retires(
    loader,
    monkeypatch,
):
    target = "lifecycle-recovery"
    live = _write_live(loader, target, version=1, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        f"{target}-update",
        target=f"auto/{target}",
        body="## Steps\n\nNEW",
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )
    recovery_pending: list[tuple[str, int]] = []
    with monkeypatch.context() as interrupted:
        interrupted.setattr(loader, "_commit_claim_consumption", lambda *_args: False)
        assert (
            loader.auto_apply_pending_update(
                f"{target}-update",
                expected_candidate_binding=binding[0],
                recovery_pending_out=recovery_pending,
            )
            is None
        )

    assert recovery_pending == [(f"auto/{target}", 2)]
    lifecycle = {
        "max_auto_skills": 100,
        "stale_after_days": 1,
        "archive_after_days": 1,
        "now": datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp(),
    }
    first = loader.run_skill_lifecycle(**lifecycle)
    assert first["archived"] == 0
    assert live.is_dir()
    assert list(loader._claims_root().glob(f"{target}-update--*"))

    loader._recover_abandoned_claims()

    assert not list(loader._claims_root().glob(f"{target}-update--*"))
    second = loader.run_skill_lifecycle(**lifecycle)
    assert second["archived"] == 1
    assert not live.exists()
    assert (loader._archive_root() / target).is_dir()


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
    assert not list(loader._evidence_root().iterdir())
    assert not list(loader._quarantine_root().iterdir())
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
    real_validate = loader._validate_and_redact_snapshot

    def reject_by_name_validation(*_args, **_kwargs):
        pytest.fail("new approval reopened the mutable claim")

    def blocking_snapshot_validation(tree, name, **kwargs):
        inspection_started.set()
        assert allow_approval.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        return real_validate(tree, name, **kwargs)

    monkeypatch.setattr(loader, "_validate_and_redact_candidate", reject_by_name_validation)
    monkeypatch.setattr(
        loader,
        "_validate_and_redact_snapshot",
        blocking_snapshot_validation,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        approval = pool.submit(loader.approve_pending_skill, "approve-replacement-race")
        assert inspection_started.wait(timeout=_THREAD_WAIT_CEILING_SECS)
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
        assert approval.result(timeout=_THREAD_WAIT_CEILING_SECS) == "auto/approve-replacement-race"

    assert "ORIGINAL" in (live / "SKILL.md").read_text(encoding="utf-8")
    assert "REPLACEMENT" not in (live / "SKILL.md").read_text(encoding="utf-8")
    assert not (live / "scripts" / "late.py").exists()
    assert (pending / "scripts" / "late.py").exists()
    claim_locks = loader._locks_root() / "claims"
    quarantines = list(loader._quarantine_root().glob("approve-replacement-race--*"))
    evidence = list(loader._evidence_root().glob("approve-replacement-race--*"))
    retained_locks = list(claim_locks.glob("approve-replacement-race--*.lock"))
    assert len(quarantines) == 1
    assert len(evidence) == 1
    assert len(retained_locks) == 1


def test_claim_fails_closed_outside_agent_denied_root(tmp_path, monkeypatch):
    unprotected_home = tmp_path / "unprotected-home"
    unprotected_home.mkdir()
    monkeypatch.setattr(skills_mod, "config_dir", lambda: unprotected_home)
    unprotected = SkillsLoader(
        skills_path=unprotected_home / "skills",
        install_builtins=False,
    )
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


def test_skill_lock_files_are_prepared_for_windows(uninitialized_loader, monkeypatch):
    loader = uninitialized_loader
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "restrict_dir_to_owner",
        lambda _path: None,
    )
    monkeypatch.setattr(
        skills_mod.platform_compat, "try_acquire_lock", lambda _fd, exclusive=False: True
    )
    monkeypatch.setattr(skills_mod.platform_compat, "release_lock", lambda _fd: None)
    _initialize_test_authority(loader)
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
    assert (loader._pending_root() / "windows-lock-byte").is_dir()
    assert not claim.exists()
    assert not (loader._evidence_root() / claim.name).exists()
    assert not (loader._quarantine_root() / claim.name).exists()


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


@pytest.mark.skipif(os.name == "nt", reason="simulated Windows race runs on POSIX")
def test_simulated_windows_staged_directory_rollback_preserves_replacement(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    _simulate_windows_native_handles(monkeypatch)
    _initialize_test_authority(loader)
    assert loader._private_state_roots_safe(create=True)
    replaced = False
    displaced: Path | None = None

    def replace_then_unlink(path, expected_identity, *, directory):
        nonlocal replaced, displaced
        path = Path(path)
        if path.name == "windows-rollback-stage" and not replaced:
            displaced = path.with_name(f"{path.name}-original")
            path.rename(displaced)
            path.mkdir()
            (path / "sentinel").write_text("NEWER DIRECTORY", encoding="utf-8")
            replaced = True
        fd = skills_mod.platform_compat.open_path_no_reparse(path)
        try:
            current_identity = skills_mod.platform_compat.opened_file_identity(fd)
        finally:
            os.close(fd)
        if current_identity != expected_identity:
            return False
        pytest.fail("replacement directory identity was accepted for unlink")

    monkeypatch.setattr(
        skills_mod.platform_compat,
        "unlink_path_if_identity",
        replace_then_unlink,
    )

    with loader._pin_private_state(create=False) as state:
        with pytest.raises(OSError, match="force staged rollback"):
            with loader._create_pinned_child_exclusive(
                state.pending,
                "windows-rollback-stage",
            ):
                raise OSError("force staged rollback")

    replacement = loader._pending_root() / "windows-rollback-stage"
    assert replaced is True
    assert displaced is not None and displaced.is_dir()
    assert (replacement / "sentinel").read_text(encoding="utf-8") == "NEWER DIRECTORY"


@pytest.mark.skipif(os.name != "nt", reason="native Windows FileIdInfo race")
def test_native_windows_staged_directory_rollback_preserves_replacement(loader, monkeypatch):
    real_unlink = skills_mod.platform_compat.unlink_path_if_identity
    replaced = False
    displaced: Path | None = None

    def replace_then_native_unlink(path, expected_identity, *, directory):
        nonlocal replaced, displaced
        path = Path(path)
        if path.name == "native-windows-rollback-stage" and not replaced:
            displaced = path.with_name(f"{path.name}-original")
            path.rename(displaced)
            path.mkdir()
            (path / "sentinel").write_text("NEWER NATIVE DIRECTORY", encoding="utf-8")
            replaced = True
        return real_unlink(path, expected_identity, directory=directory)

    monkeypatch.setattr(
        skills_mod.platform_compat,
        "unlink_path_if_identity",
        replace_then_native_unlink,
    )

    # ``_pin_private_state(create=False)`` pins existing roots only; the simulated
    # sibling above materializes them first, and so must this one.
    assert loader._private_state_roots_safe(create=True)
    with loader._pin_private_state(create=False) as state:
        with pytest.raises(OSError, match="force native rollback"):
            with loader._create_pinned_child_exclusive(
                state.pending,
                "native-windows-rollback-stage",
            ):
                raise OSError("force native rollback")

    replacement = loader._pending_root() / "native-windows-rollback-stage"
    assert replaced is True
    assert displaced is not None and displaced.is_dir()
    assert (replacement / "sentinel").read_text(encoding="utf-8") == ("NEWER NATIVE DIRECTORY")


def test_windows_private_cleanup_retries_readonly_regular_file(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    _simulate_windows_native_handles(monkeypatch)
    _initialize_test_authority(loader)
    assert loader._private_state_roots_safe(create=True) is True
    target = loader._claims_root() / "readonly-private-file"
    target.write_bytes(b"private")
    target.chmod(stat.S_IREAD)
    calls = 0

    def identity_bound_unlink(path, expected_identity, *, directory):
        nonlocal calls
        calls += 1
        path = Path(path)
        fd = skills_mod.platform_compat.open_path_no_reparse(path)
        try:
            current_identity = skills_mod.platform_compat.opened_file_identity(fd)
        finally:
            os.close(fd)
        if current_identity != expected_identity:
            return False
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
        path.unlink()
        return True

    monkeypatch.setattr(
        skills_mod.platform_compat,
        "unlink_path_if_identity",
        identity_bound_unlink,
    )

    with loader._pin_private_state(create=False) as private_state:
        expected = loader._stat_pinned_child(private_state.claims, target.name)
        expected_identity = loader._pinned_child_identity(private_state.claims, target.name)
        assert expected_identity is not None
        assert loader._unlink_skill_child(
            private_state.claims,
            target.name,
            expected=expected,
            expected_identity=expected_identity,
        )

    assert calls == 1
    assert not target.exists()


def test_windows_unstable_crt_projection_does_not_break_promotion_or_restart_restore(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "restrict_dir_to_owner",
        lambda _path: None,
    )
    _initialize_test_authority(loader)
    real_fstat = skills_mod.os.fstat
    projection = 0

    def unstable_fstat(fd):
        nonlocal projection
        result = real_fstat(fd)
        projection += 1
        return _restat(result, st_ino=int(result.st_ino) + projection)

    monkeypatch.setattr(skills_mod.os, "fstat", unstable_fstat)
    target = "windows-unstable-end-to-end"
    _write_live(loader, target, version=1, body="BEFORE")
    _stage_update(loader, f"{target}-update", target=f"auto/{target}")
    assert loader.approve_pending_update(f"{target}-update") == f"auto/{target}"

    pending_slug = "windows-unstable-restart"
    loader.stage_skill_candidate(
        pending_slug,
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nrestore",
        provenance=_prov(),
    )
    claimed = loader._claim_pending_update(pending_slug)
    assert claimed is not None
    claim, fd, _consumed_at, _snapshot = claimed
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert [row["slug"] for row in restarted.list_pending_skills()] == [pending_slug]
    assert not claim.exists()


def test_private_state_migration_refuses_cross_device(uninitialized_loader, monkeypatch):
    loader = uninitialized_loader
    legacy = loader._legacy_private_root()
    legacy.mkdir(parents=True)
    sentinel = legacy / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    real_pin_child = loader._pin_skill_child_parent

    @contextlib.contextmanager
    def cross_device_legacy(parent, name, *, create, created_out=None):
        with real_pin_child(
            parent,
            name,
            create=create,
            created_out=created_out,
        ) as child:
            if parent.path == legacy.parent and name == skills_mod.AUTO_PRIVATE_DIRNAME:
                child = skills_mod._PinnedSkillParent(
                    child.path,
                    child.fd,
                    child.identity,
                    skills_mod._TaggedFileIdentity(
                        child.native_identity.kind,
                        child.native_identity.volume + 1,
                        child.native_identity.object_id,
                    ),
                )
            yield child

    monkeypatch.setattr(loader, "_pin_skill_child_parent", cross_device_legacy)

    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is False
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not loader._private_root().exists()


def test_populated_legacy_private_state_refuses_untouched(uninitialized_loader):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    legacy = loader._legacy_private_root()
    claim = legacy / "claims" / "candidate--token"
    claim.mkdir(parents=True)
    sentinel = claim / "SKILL.md"
    sentinel.write_text("retained authority", encoding="utf-8")
    legacy_identity = legacy.stat().st_ino

    with pytest.raises(
        OSError,
        match="populated legacy auto-skill private state requires stopped-installation migration",
    ):
        skills_mod.initialize_auto_skill_private_authority(
            data_home=home,
            configured_home=home,
        )

    assert legacy.stat().st_ino == legacy_identity
    assert sentinel.read_text(encoding="utf-8") == "retained authority"
    assert not loader._private_root().exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX retains parent traversal on open dirs")
def test_empty_legacy_state_refuses_while_retained_descriptor_reaches_parent(
    uninitialized_loader,
):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    legacy = loader._legacy_private_root()
    legacy.mkdir(parents=True)
    retained_fd = os.open(legacy, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    legacy_identity = os.fstat(retained_fd).st_ino
    auto_identity = legacy.parent.stat().st_ino
    try:
        with pytest.raises(
            OSError,
            match="empty legacy auto-skill private state requires stopped-installation migration",
        ):
            skills_mod.initialize_auto_skill_private_authority(
                data_home=home,
                configured_home=home,
            )
        assert legacy.stat().st_ino == legacy_identity
        assert not loader._private_root().exists()

        parent_fd = os.open(
            "..",
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            dir_fd=retained_fd,
        )
        try:
            assert os.fstat(parent_fd).st_ino == auto_identity
        finally:
            os.close(parent_fd)
    finally:
        os.close(retained_fd)


def test_transitional_direct_private_state_refuses_untouched(uninitialized_loader):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    direct = home / skills_mod.AUTO_SKILL_PRIVATE_STATE_DIRNAME
    direct.mkdir()
    sentinel = direct / "sentinel"
    sentinel.write_text("retained direct authority", encoding="utf-8")
    direct_identity = direct.stat().st_ino

    with pytest.raises(
        OSError,
        match="populated transitional direct auto-skill private state requires "
        "stopped-installation recovery",
    ):
        skills_mod.initialize_auto_skill_private_authority(
            data_home=home,
            configured_home=home,
        )

    assert direct.stat().st_ino == direct_identity
    assert sentinel.read_text(encoding="utf-8") == "retained direct authority"
    assert not loader._private_root().exists()


def test_no_legacy_state_builds_the_ordinary_authority_hierarchy(loader):
    assert not os.path.lexists(loader._legacy_private_root())

    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is True

    assert loader._private_root().is_dir()
    assert loader._authority_provenance_path().is_file()
    assert loader._claims_root().is_dir()
    assert loader._evidence_root().is_dir()
    assert (loader._locks_root() / skills_mod.AUTO_CLAIMS_DIRNAME).is_dir()


@pytest.mark.parametrize(
    "record_case",
    [
        "missing",
        "inside-only",
        "malformed",
        "legacy-v1",
        "bad-mac",
        "home-mismatch",
        "root-mismatch",
    ],
)
def test_existing_authority_without_matching_external_provenance_refuses_untouched(
    uninitialized_loader,
    record_case,
):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    root = loader._private_root()
    root.mkdir(parents=True)
    sentinel = root / "sentinel"
    sentinel.write_text("DO NOT TOUCH", encoding="utf-8")
    root_identity = root.stat().st_ino
    provenance = loader._authority_provenance_path()

    if record_case == "inside-only":
        (root / skills_mod._AUTHORITY_PROVENANCE_NAME).write_text("forged", encoding="utf-8")
    elif record_case != "missing":
        provenance.parent.mkdir(parents=True, exist_ok=True)
        if record_case == "malformed":
            provenance.write_text("{", encoding="utf-8")
        else:
            with (
                loader._pin_skill_parent(home) as home_parent,
                loader._pin_skill_parent(root) as root_parent,
            ):
                home_identity = home_parent.native_identity
                authority_identity = root_parent.native_identity
            if record_case == "home-mismatch":
                home_identity = _different_identity(home_identity)
            if record_case == "root-mismatch":
                authority_identity = _different_identity(authority_identity)
            if record_case == "legacy-v1":
                body = {
                    "version": 1,
                    "selected_home": loader._identity_payload(home_identity),
                    "authority_root": loader._identity_payload(authority_identity),
                }
            else:
                body = loader._authority_record_body(
                    home,
                    home,
                    home_identity,
                    authority_identity,
                )
            body["mac"] = (
                "0" * 64 if record_case == "bad-mac" else loader._authority_record_mac(body)
            )
            provenance.write_text(json.dumps(body), encoding="utf-8")

    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is False
    assert root.stat().st_ino == root_identity
    assert sentinel.read_text(encoding="utf-8") == "DO NOT TOUCH"


@pytest.mark.skipif(os.name == "nt", reason="simulated Windows race runs on POSIX")
@pytest.mark.parametrize("record_case", ["home-mismatch", "root-mismatch"])
def test_simulated_windows_identity_mismatch_refuses_untouched(
    uninitialized_loader,
    monkeypatch,
    record_case,
):
    """The mismatch cases again, with native 128-bit file ids instead of inodes.

    Native Windows hands the provenance record a 16-byte file id. A fixture that
    can only perturb an integer inode never reaches that record shape on POSIX.
    """
    _simulate_windows_native_handles(monkeypatch)
    test_existing_authority_without_matching_external_provenance_refuses_untouched(
        uninitialized_loader,
        record_case,
    )


def test_authority_creation_crash_leaves_uncertified_root_inert(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    root = loader._private_root()
    provenance = loader._authority_provenance_path()
    real_write = loader._write_pinned_new_file

    def fail_provenance(parent, name, payload, **kwargs):
        if name == skills_mod._AUTHORITY_PROVENANCE_NAME:
            raise OSError("injected provenance write crash")
        return real_write(parent, name, payload, **kwargs)

    with monkeypatch.context() as crash:
        crash.setattr(loader, "_write_pinned_new_file", fail_provenance)
        with pytest.raises(OSError):
            loader._ensure_private_authority(
                root.parents[1],
                configured_home=root.parents[1],
                create=True,
            )

    assert root.is_dir()
    root_identity = root.stat().st_ino
    assert not provenance.exists()
    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is False
    assert root.stat().st_ino == root_identity
    assert not provenance.exists()


def test_ordinary_operation_before_initializer_creates_no_authority(uninitialized_loader):
    loader = uninitialized_loader
    root = loader._private_root()
    provenance = loader._authority_provenance_path()

    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is False
    assert (
        loader.stage_skill_candidate(
            "before-initializer",
            description="candidate",
            triggers="candidate",
            procedure_md="## Steps\n\nwait",
            provenance=_prov(),
        )
        is None
    )
    assert not root.exists()
    assert not provenance.exists()


def test_valid_authority_provenance_reuses_exact_root(loader):
    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is True
    root_identity = loader._private_root().stat().st_ino
    record = loader._authority_provenance_path().read_bytes()

    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is True
    assert loader._private_root().stat().st_ino == root_identity
    assert loader._authority_provenance_path().read_bytes() == record


def test_root_swap_after_provenance_before_use_refuses(loader, monkeypatch):
    root = loader._private_root()
    displaced = root.with_name("auto-skill-private-after-verify")
    real_require = skills_mod.require_auto_skill_private_authority
    swapped = False

    def require_then_swap():
        nonlocal swapped
        binding = real_require()
        if not swapped:
            root.rename(displaced)
            root.mkdir()
            (root / "replacement").write_text("KEEP", encoding="utf-8")
            swapped = True
        return binding

    monkeypatch.setattr(skills_mod, "require_auto_skill_private_authority", require_then_swap)
    try:
        assert loader._private_state_roots_safe(create=True, require_sensitive=True) is False
        assert swapped is True
        assert (root / "replacement").read_text(encoding="utf-8") == "KEEP"
        assert not (root / skills_mod.AUTO_CLAIMS_DIRNAME).exists()
    finally:
        if swapped:
            if root.exists():
                shutil.rmtree(root)
            if displaced.exists():
                displaced.rename(root)


@requires_renamable_pinned_directory
def test_fresh_root_swap_after_provenance_sync_refuses(uninitialized_loader, monkeypatch):
    loader = uninitialized_loader
    root = loader._private_root()
    displaced = root.with_name("auto-skill-private-after-sync")
    real_sync = loader._sync_pinned_parent
    swapped = False

    def sync_then_swap(parent):
        nonlocal swapped
        result = real_sync(parent)
        if (
            not swapped
            and parent.path.name == skills_mod._AUTHORITY_PROVENANCE_PARENT
            and loader._authority_provenance_path().exists()
        ):
            root.rename(displaced)
            root.mkdir()
            (root / "replacement").write_text("KEEP", encoding="utf-8")
            swapped = True
        return result

    monkeypatch.setattr(loader, "_sync_pinned_parent", sync_then_swap)
    with pytest.raises(OSError, match="changed after provenance durability"):
        loader._ensure_private_authority(
            root.parents[1],
            configured_home=root.parents[1],
            create=True,
        )
    try:
        assert swapped is True
        assert (root / "replacement").read_text(encoding="utf-8") == "KEEP"
        assert displaced.is_dir()
    finally:
        shutil.rmtree(root)
        displaced.rename(root)


def test_selected_home_symlink_retarget_refuses_new_target(tmp_path, monkeypatch):
    configured = tmp_path / "configured-home"
    first = tmp_path / "home-a"
    second = tmp_path / "home-b"
    first.mkdir()
    second.mkdir()
    configured.symlink_to(first, target_is_directory=True)
    monkeypatch.setattr(skills_mod, "config_dir", lambda: configured)
    denied = {
        _authority_path(first),
        first / skills_mod._AUTHORITY_PROVENANCE_PARENT / skills_mod._AUTHORITY_PROVENANCE_NAME,
        _authority_path(second),
        second / skills_mod._AUTHORITY_PROVENANCE_PARENT / skills_mod._AUTHORITY_PROVENANCE_NAME,
    }
    monkeypatch.setattr(skills_mod, "is_sensitive_path", lambda path: Path(path) in denied)

    binding = skills_mod.initialize_auto_skill_private_authority(
        data_home=configured,
        configured_home=configured,
    )
    assert binding.canonical_home == first

    configured.unlink()
    configured.symlink_to(second, target_is_directory=True)
    with pytest.raises(OSError, match="path or identity changed"):
        skills_mod.initialize_auto_skill_private_authority(
            data_home=configured,
            configured_home=configured,
        )
    assert not _authority_path(second).exists()
    assert not (
        second / skills_mod._AUTHORITY_PROVENANCE_PARENT / skills_mod._AUTHORITY_PROVENANCE_NAME
    ).exists()


def test_selected_home_same_inode_relocation_refuses_retarget(tmp_path, monkeypatch):
    configured = tmp_path / "configured-home"
    first = tmp_path / "home-a"
    second = tmp_path / "home-b"
    first.mkdir()
    configured.symlink_to(first, target_is_directory=True)
    monkeypatch.setattr(skills_mod, "config_dir", lambda: configured)
    denied = {
        _authority_path(first),
        first / skills_mod._AUTHORITY_PROVENANCE_PARENT / skills_mod._AUTHORITY_PROVENANCE_NAME,
        _authority_path(second),
        second / skills_mod._AUTHORITY_PROVENANCE_PARENT / skills_mod._AUTHORITY_PROVENANCE_NAME,
    }
    monkeypatch.setattr(skills_mod, "is_sensitive_path", lambda path: Path(path) in denied)

    binding = skills_mod.initialize_auto_skill_private_authority(
        data_home=configured,
        configured_home=configured,
    )
    home_identity = first.stat().st_ino
    root_identity = _authority_path(first).stat().st_ino

    first.rename(second)
    first.symlink_to(second, target_is_directory=True)

    assert second.stat().st_ino == home_identity
    assert _authority_path(second).stat().st_ino == root_identity
    with pytest.raises(OSError, match="retargeted after startup certification"):
        skills_mod.verify_auto_skill_private_authority(binding)
    assert _authority_path(second).is_dir()


def test_certified_authority_refuses_root_replacement(loader):
    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is True
    root = loader._private_root()
    displaced = root.with_name("auto-skill-private-certified")
    root.rename(displaced)
    root.mkdir()
    sentinel = root / "replacement"
    sentinel.write_text("KEEP", encoding="utf-8")
    try:
        assert loader._private_state_roots_safe(create=True, require_sensitive=True) is False
        assert sentinel.read_text(encoding="utf-8") == "KEEP"
        assert displaced.is_dir()
    finally:
        root.rmdir() if not any(root.iterdir()) else shutil.rmtree(root)
        displaced.rename(root)


def test_certified_authority_refuses_selected_home_replacement(loader):
    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is True
    home = loader._private_root().parents[1]
    displaced = home.with_name(f"{home.name}-certified")
    home.rename(displaced)
    home.mkdir()
    try:
        assert loader._private_state_roots_safe(create=True, require_sensitive=True) is False
        assert list(home.iterdir()) == []
        assert _authority_path(displaced).is_dir()
    finally:
        home.rmdir()
        displaced.rename(home)


@pytest.mark.parametrize(
    "identity",
    [
        skills_mod._TaggedFileIdentity("posix-dev-ino", 7, 11),
        skills_mod._TaggedFileIdentity("windows-file-id-128", 13, bytes(range(16))),
    ],
)
def test_tagged_identity_payload_round_trips(identity):
    payload = SkillsLoader._identity_payload(identity)
    assert SkillsLoader._identity_from_payload(payload) == identity


@pytest.mark.parametrize(
    "payload",
    [None, [1, 2], {"kind": "windows-file-id-128", "volume": 1, "file_id": "AA=="}],
)
def test_legacy_or_malformed_identity_payload_fails_closed(payload):
    assert SkillsLoader._identity_from_payload(payload) is None
    fields = {
        "claim_generation": "a" * 64,
        "claim_metadata": None,
        "quarantine_identity": payload,
    }
    assert SkillsLoader._claim_snapshot_from_fields(fields) is None


@pytest.mark.parametrize(
    "identity",
    [
        skills_mod._TaggedFileIdentity("posix-dev-ino", 17, 19),
        skills_mod._TaggedFileIdentity("windows-file-id-128", 23, b"w" * 16),
    ],
)
def test_claim_snapshot_json_round_trips_tagged_identity(identity):
    snapshot = skills_mod._ClaimSnapshot("b" * 64, b"{}", quarantine_identity=identity)
    fields = SkillsLoader._claim_snapshot_fields(snapshot)
    decoded = json.loads(json.dumps(fields))
    assert SkillsLoader._claim_snapshot_from_fields(decoded) == snapshot


def test_simulated_windows_rename_uses_native_identity_not_crt_samestat(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "restrict_dir_to_owner",
        lambda _path: None,
    )
    _initialize_test_authority(loader)
    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is True
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        source = state.claims.path / "native-rename-source"
        source.mkdir()
        expected = _tagged_identity_for_path(loader, source)
        monkeypatch.setattr(
            skills_mod.os.path,
            "samestat",
            lambda *_args: pytest.fail("CRT samestat became Windows rename authority"),
        )
        loader._rename_skill_child_no_replace(
            state.claims,
            source.name,
            state.evidence,
            "native-rename-destination",
            expected_identity=expected,
        )
        assert (
            loader._pinned_child_identity(
                state.evidence,
                "native-rename-destination",
            )
            == expected
        )


def test_simulated_windows_rename_refuses_changed_native_destination(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "restrict_dir_to_owner",
        lambda _path: None,
    )
    _initialize_test_authority(loader)
    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is True
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        source = state.claims.path / "native-collision-source"
        source.mkdir()
        expected = _tagged_identity_for_path(loader, source)
        real_identity = loader._pinned_child_identity

        def replaced_identity(parent, name):
            identity = real_identity(parent, name)
            if parent.path == state.evidence.path and name == "native-collision-destination":
                return skills_mod._TaggedFileIdentity(
                    "windows-file-id-128",
                    expected.volume,
                    b"x" * 16,
                )
            return identity

        monkeypatch.setattr(loader, "_pinned_child_identity", replaced_identity)
        with pytest.raises(OSError, match="changed during rename"):
            loader._rename_skill_child_no_replace(
                state.claims,
                source.name,
                state.evidence,
                "native-collision-destination",
                expected_identity=expected,
            )
        assert not (state.evidence.path / "native-collision-destination").exists()
        assert (state.claims.path / source.name).is_dir()


def test_private_roots_refuse_linked_auto_namespace_ancestor(
    loader,
    tmp_path,
    caplog,
):
    """A symlinked ``auto`` namespace must fail closed before any mkdir.

    Certified first: with no certificate the refusal is the missing authority,
    which must not be reported with the link remedy.
    """
    auto_root = loader._dir / skills_mod.AUTO_SKILL_NAMESPACE
    loader._dir.mkdir(parents=True, exist_ok=True)
    if auto_root.exists():
        shutil.rmtree(auto_root)
    elsewhere = tmp_path / "elsewhere-auto"
    elsewhere.mkdir()
    auto_root.symlink_to(elsewhere, target_is_directory=True)
    try:
        assert loader._private_state_roots_safe(create=True) is False
        # Nothing may be created after public ancestry fails authentication.
        assert not loader._claims_root().exists()
        assert "link the Kiro Crew data-home root instead" in caplog.text
    finally:
        auto_root.unlink()


def test_a_missing_certificate_is_not_reported_as_a_link_problem(uninitialized_loader, caplog):
    assert uninitialized_loader._private_state_roots_safe(create=True) is False
    assert "Remove links or junctions" not in caplog.text
    assert "no gateway-startup certificate" in caplog.text


def test_private_roots_refuse_linked_pending_ancestor(uninitialized_loader, tmp_path):
    loader = uninitialized_loader
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


def test_private_roots_allow_linked_operator_prefix(tmp_path, monkeypatch):
    """Links ABOVE the skills dir (operator-controlled) must stay allowed."""
    real_home = tmp_path / "real-home"
    (real_home / "skills").mkdir(parents=True)
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    denied = {
        _authority_path(real_home),
        real_home / skills_mod._AUTHORITY_PROVENANCE_PARENT / skills_mod._AUTHORITY_PROVENANCE_NAME,
    }
    monkeypatch.setattr(skills_mod, "is_sensitive_path", lambda path: Path(path) in denied)
    monkeypatch.setattr(skills_mod, "config_dir", lambda: linked_home)
    loader = skills_mod.SkillsLoader(linked_home / "skills", install_builtins=False)
    binding = skills_mod.initialize_auto_skill_private_authority(
        skills_root=loader._dir,
        data_home=linked_home,
        configured_home=linked_home,
    )
    assert binding.canonical_home == real_home
    skills_mod.verify_auto_skill_private_authority(binding)


def test_private_root_creation_refuses_swapped_data_home(
    uninitialized_loader,
    monkeypatch,
    private_state_mode,
):
    loader = uninitialized_loader
    loader._dir.mkdir(parents=True, exist_ok=True)
    auto_root = loader._dir / skills_mod.AUTO_SKILL_NAMESPACE
    auto_root.mkdir()
    (auto_root / skills_mod.AUTO_PENDING_DIRNAME).mkdir()
    data_home = loader._private_root().parents[1]
    displaced = data_home.with_name(f"{data_home.name}-before-private-swap-{private_state_mode}")
    real_pin_child = loader._pin_skill_child_parent
    swapped = False

    @contextlib.contextmanager
    def swap_before_private_create(parent, name, *, create, created_out=None):
        nonlocal swapped
        if name == skills_mod.AUTO_SKILL_PRIVATE_STATE_DIRNAME and not swapped:
            data_home.rename(displaced)
            swapped = True
            data_home.mkdir(exist_ok=True)
        with real_pin_child(
            parent,
            name,
            create=create,
            created_out=created_out,
        ) as child:
            yield child

    monkeypatch.setattr(loader, "_pin_skill_child_parent", swap_before_private_create)

    with pytest.raises(OSError):
        loader._ensure_private_authority(
            data_home,
            configured_home=data_home,
            create=True,
        )
    if swapped:
        assert (displaced / "skills/auto/.pending").is_dir()
        assert not _authority_path(displaced).exists()
        assert not loader._private_root().exists()
    else:
        assert not displaced.exists()
        assert (data_home / "skills/auto/.pending").is_dir()
        assert not loader._private_root().exists()


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
    if private_state_mode == "fallback":
        # POSIX stages only descriptor-relative, so the candidate is staged with the
        # host's real primitives before claims fall back. Windows has no dir_fd and
        # stages by name, so forcing True there reaches POSIX-only opens.
        with pytest.MonkeyPatch.context() as staging_patch:
            staging_patch.setattr(skills_mod, "_DIR_FD_SUPPORTED", _HOST_DIR_FD_SUPPORTED)
            _stage_update(loader, slug, target="auto/claim-target")
    else:
        _stage_update(loader, slug, target="auto/claim-target")
    claims_root = loader._claims_root()
    displaced = claims_root.with_name("claims-before-swap")
    real_rename = loader._rename_skill_child_no_replace
    swapped = False

    def swap_before_claim_create(source, source_name, destination, destination_name, **kwargs):
        nonlocal swapped
        if destination_name.startswith(f"{slug}--") and not swapped:
            claims_root.rename(displaced)
            claims_root.mkdir()
            swapped = True
        return real_rename(source, source_name, destination, destination_name, **kwargs)

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


def test_linked_claim_dismissal_unlink_failure_restores_exact_link(loader, monkeypatch):
    slug = "linked-dismissal-failure"
    link, victim, metadata = _linked_pending_candidate(loader, slug)
    real_unlink = loader._unlink_skill_child

    def fail_quarantine_unlink(parent, name, **kwargs):
        if parent.path == loader._quarantine_root() and name.startswith(f"{slug}--"):
            return False
        return real_unlink(parent, name, **kwargs)

    monkeypatch.setattr(loader, "_unlink_skill_child", fail_quarantine_unlink)

    assert loader.dismiss_pending_skill(slug) is False

    assert skills_mod.platform_compat.is_link_or_junction(link)
    assert (victim / "SKILL.md").read_text(encoding="utf-8") == "VICTIM"
    assert (victim / ".meta.json").read_text(encoding="utf-8") == metadata
    assert not (victim / ".promoted").exists()
    assert not list(loader._quarantine_root().iterdir())


@pytest.mark.skipif(os.name == "nt", reason="simulated Windows race runs on POSIX")
def test_simulated_windows_linked_dismissal_preserves_replacement(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    _simulate_windows_native_handles(monkeypatch)
    _initialize_test_authority(loader)
    assert loader._private_state_roots_safe(create=True)
    slug = "windows-linked-dismissal-replacement"
    _link, victim, _metadata = _linked_pending_candidate(loader, slug)
    replaced = False
    real_unlink = skills_mod.platform_compat.unlink_path_if_identity

    def replace_then_unlink(path, expected_identity, *, directory):
        nonlocal replaced
        path = Path(path)
        if path.parent != loader._quarantine_root():
            return real_unlink(path, expected_identity, directory=directory)
        if not replaced:
            original = path.with_name(f"{path.name}-original")
            path.rename(original)
            path.write_text("NEWER QUARANTINE STATE", encoding="utf-8")
            replaced = True
        fd = skills_mod.platform_compat.open_path_no_reparse(path)
        try:
            current_identity = skills_mod.platform_compat.opened_file_identity(fd)
        finally:
            os.close(fd)
        if current_identity != expected_identity:
            return False
        pytest.fail("replacement identity was accepted for unlink")

    monkeypatch.setattr(
        skills_mod.platform_compat,
        "unlink_path_if_identity",
        replace_then_unlink,
    )

    assert loader.dismiss_pending_skill(slug) is False
    assert replaced is True
    quarantined = list(loader._quarantine_root().glob(f"{slug}--*"))
    replacements = [entry for entry in quarantined if entry.is_file() and not entry.is_symlink()]
    originals = [
        entry for entry in quarantined if skills_mod.platform_compat.is_link_or_junction(entry)
    ]
    assert len(replacements) == 1
    assert len(originals) == 1
    assert replacements[0].read_text(encoding="utf-8") == "NEWER QUARANTINE STATE"
    assert (victim / "SKILL.md").read_text(encoding="utf-8") == "VICTIM"


@pytest.mark.skipif(
    not hasattr(os, "O_PATH"),
    reason="models macOS's open flags with Linux's O_PATH; macOS runs the test above itself",
)
def test_simulated_windows_linked_dismissal_on_macos_open_flags(
    uninitialized_loader,
    monkeypatch,
):
    """The shim's link-opening branch for a host without O_PATH, run on Linux.

    macOS has no O_PATH and opens a link itself only through O_SYMLINK. Modelled
    here as that flag set, the simulated linked dismissal must still reach the
    quarantine unlink rather than refusing at the first open of the link.
    """
    open_link_itself = os.O_PATH | os.O_NOFOLLOW
    monkeypatch.delattr(os, "O_PATH")
    monkeypatch.setattr(os, "O_SYMLINK", open_link_itself, raising=False)
    test_simulated_windows_linked_dismissal_preserves_replacement(
        uninitialized_loader,
        monkeypatch,
    )


@pytest.mark.skipif(os.name != "nt", reason="native Windows FileIdInfo race")
def test_native_windows_linked_dismissal_preserves_replacement(loader, monkeypatch):
    slug = "native-windows-linked-dismissal"
    _link, victim, _metadata = _linked_pending_candidate(loader, slug)
    real_unlink = skills_mod.platform_compat.unlink_path_if_identity
    replaced = False

    def replace_then_native_unlink(path, expected_identity, *, directory):
        nonlocal replaced
        path = Path(path)
        if path.parent == loader._quarantine_root() and not replaced:
            original = path.with_name(f"{path.name}-original")
            path.rename(original)
            path.write_text("NEWER NATIVE WINDOWS STATE", encoding="utf-8")
            replaced = True
        return real_unlink(path, expected_identity, directory=directory)

    monkeypatch.setattr(
        skills_mod.platform_compat,
        "unlink_path_if_identity",
        replace_then_native_unlink,
    )

    assert loader.dismiss_pending_skill(slug) is False
    assert replaced is True
    quarantined = list(loader._quarantine_root().glob(f"{slug}--*"))
    replacements = [entry for entry in quarantined if entry.is_file() and not entry.is_symlink()]
    originals = [
        entry for entry in quarantined if skills_mod.platform_compat.is_link_or_junction(entry)
    ]
    assert len(replacements) == 1
    assert len(originals) == 1
    assert replacements[0].read_text(encoding="utf-8") == "NEWER NATIVE WINDOWS STATE"
    assert (victim / "SKILL.md").read_text(encoding="utf-8") == "VICTIM"


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


def test_failed_prepared_restore_discards_mutated_trusted_materialization(loader, monkeypatch):
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
        (claim / "SKILL.md").write_text("MUTATED TRUSTED COPY\n", encoding="utf-8")
        mutated_claims.append(claim)
        return "incomplete"

    monkeypatch.setattr(loader, "_publish_prepared_skill_tree", mutate_claim_and_refuse)

    assert loader.approve_pending_update(slug) is None

    pending = loader._pending_root() / slug
    assert "new steps" in (pending / "SKILL.md").read_text(encoding="utf-8")
    assert (pending / "scripts" / "safe.py").read_text(encoding="utf-8") == "print('safe')\n"
    assert len(mutated_claims) == 1
    claim = mutated_claims[0]
    assert not claim.exists()
    assert not (loader._evidence_root() / claim.name).exists()
    assert not (loader._quarantine_root() / claim.name).exists()
    assert not loader._claim_lock_path(claim.name).exists()


@pytest.mark.skipif(os.name == "nt", reason="retained POSIX descriptors survive rename")
@pytest.mark.parametrize("late_bytes", [b"LATE PUBLIC WRITE\n", b""])
def test_failure_restoration_preserves_late_writes_and_concurrent_replacement(
    loader,
    late_bytes,
):
    slug = "restore-exact-inode"
    _stage_update(loader, slug, target="auto/restore-target")
    pending = loader._pending_root() / slug
    retained_fd = os.open(pending / "SKILL.md", os.O_WRONLY)
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, snapshot = claimed
    quarantine = loader._quarantine_root() / claim.name
    quarantine_identity = os.stat(quarantine)
    os.lseek(retained_fd, 0, os.SEEK_SET)
    os.ftruncate(retained_fd, 0)
    os.write(retained_fd, late_bytes)
    os.fsync(retained_fd)
    os.close(retained_fd)
    pending.mkdir()
    (pending / "SKILL.md").write_text("CONCURRENT REPLACEMENT\n", encoding="utf-8")
    try:
        restored = loader._restore_claimed_update(claim, fd, slug, snapshot)
    finally:
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    assert restored == loader._pending_root() / f"{slug}-2"
    assert (pending / "SKILL.md").read_text(encoding="utf-8") == "CONCURRENT REPLACEMENT\n"
    assert (restored / "SKILL.md").read_bytes() == late_bytes
    assert os.path.samestat(quarantine_identity, os.stat(restored))
    assert not quarantine.exists()
    assert not claim.exists()


def test_recovery_never_adopts_unjournaled_public_quarantine(loader):
    assert loader._private_state_roots_safe(create=True) is True
    planted = loader._quarantine_root() / "planted--0123456789abcdef"
    planted.mkdir()
    (planted / "SKILL.md").write_text("UNTRUSTED\n", encoding="utf-8")

    loader._recover_abandoned_claims()

    assert planted.is_dir()
    assert not (loader._pending_root() / "planted").exists()
    assert not loader._claim_lock_path(planted.name).exists()
    assert not (loader._claims_root() / planted.name).exists()
    assert not (loader._evidence_root() / planted.name).exists()


@pytest.mark.parametrize(
    "slug",
    ["quarantine-receipt-recovery", "quarantine--receipt-recovery"],
)
def test_restart_finishes_quarantine_restore_receipt(loader, slug):
    _stage_update(loader, slug, target="auto/recovery-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, _snapshot = claimed
    quarantine = loader._quarantine_root() / claim.name
    lock_path = loader._claim_lock_path(claim.name)
    assert loader._write_claim_restore_state(
        fd,
        lock_path,
        claim.name,
        restore_slug=slug,
        restore_identity=_tagged_identity_for_path(
            loader,
            loader._quarantine_root() / claim.name,
        ),
    )
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert [row["slug"] for row in restarted.list_pending_skills()] == [slug]
    assert not quarantine.exists()
    assert not claim.exists()
    assert not lock_path.exists()


def test_restart_honors_recorded_alternate_after_original_slot_frees(loader):
    slug = "recorded-alternate-free-original"
    restore_slug = f"{slug}-2"
    _stage_update(loader, slug, target="auto/recovery-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, _snapshot = claimed
    quarantine = loader._quarantine_root() / claim.name
    quarantine_info = os.stat(quarantine)
    original = loader._pending_root() / slug
    original.mkdir()
    (original / "SKILL.md").write_text("TEMPORARY OCCUPANT\n", encoding="utf-8")
    assert loader._write_claim_restore_state(
        fd,
        loader._claim_lock_path(claim.name),
        claim.name,
        restore_slug=restore_slug,
        restore_identity=_tagged_identity_for_path(
            loader,
            loader._quarantine_root() / claim.name,
        ),
    )
    shutil.rmtree(original)
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert [row["slug"] for row in restarted.list_pending_skills()] == [restore_slug]
    restored = restarted._pending_root() / restore_slug
    assert os.path.samestat(quarantine_info, os.stat(restored))
    assert not original.exists()
    assert not quarantine.exists()
    assert not claim.exists()


def test_restart_recorded_destination_collision_has_no_fallback(loader):
    slug = "recorded-alternate-occupied"
    restore_slug = f"{slug}-2"
    _stage_update(loader, slug, target="auto/recovery-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, _snapshot = claimed
    quarantine = loader._quarantine_root() / claim.name
    quarantine_info = os.stat(quarantine)
    assert loader._write_claim_restore_state(
        fd,
        loader._claim_lock_path(claim.name),
        claim.name,
        restore_slug=restore_slug,
        restore_identity=_tagged_identity_for_path(
            loader,
            loader._quarantine_root() / claim.name,
        ),
    )
    occupant = loader._pending_root() / restore_slug
    occupant.mkdir()
    sentinel = occupant / "SKILL.md"
    sentinel.write_text("DO NOT REPLACE\n", encoding="utf-8")
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    restarted.list_pending_skills()

    assert sentinel.read_text(encoding="utf-8") == "DO NOT REPLACE\n"
    assert os.path.samestat(quarantine_info, os.stat(quarantine))
    assert claim.is_dir()
    assert not (restarted._pending_root() / slug).exists()
    assert not (restarted._pending_root() / f"{slug}-3").exists()
    assert restarted._claim_lock_path(claim.name).is_file()


@pytest.mark.parametrize(
    "slug",
    ["quarantine-before-materialization", "quarantine--before-materialization"],
)
def test_restart_restores_quarantine_when_private_materialization_is_missing(loader, slug):
    _stage_update(loader, slug, target="auto/recovery-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, _snapshot = claimed
    quarantine = loader._quarantine_root() / claim.name
    assert loader._discard_trusted_claim(claim) is True
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert [row["slug"] for row in restarted.list_pending_skills()] == [slug]
    assert not quarantine.exists()
    assert not claim.exists()
    assert not restarted._claim_lock_path(claim.name).exists()


def test_private_evidence_refuses_same_name_quarantine_replacement(loader):
    slug = "quarantine-identity-replacement"
    _stage_update(loader, slug, target="auto/identity-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, _snapshot = claimed
    quarantine = loader._quarantine_root() / claim.name
    detached = quarantine.with_name(f"{quarantine.name}.detached")
    quarantine.rename(detached)
    quarantine.mkdir()
    (quarantine / "SKILL.md").write_text("REPLACEMENT\n", encoding="utf-8")
    try:
        assert loader._retain_claim_evidence(claim, fd) is False
    finally:
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    assert claim.is_dir()
    assert detached.is_dir()
    assert quarantine.is_dir()
    assert not (loader._evidence_root() / claim.name).exists()
    assert loader._claim_lock_path(claim.name).is_file()


@pytest.mark.parametrize(
    "slug",
    ["evidence-name-collision", "evidence--name-collision"],
)
def test_private_evidence_collision_preserves_claim_quarantine_and_collision(loader, slug):
    _stage_update(loader, slug, target="auto/evidence-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, _snapshot = claimed
    quarantine = loader._quarantine_root() / claim.name
    collision = loader._evidence_root() / claim.name
    collision.mkdir()
    sentinel = collision / "sentinel"
    sentinel.write_text("DO NOT TOUCH", encoding="utf-8")
    try:
        assert loader._commit_claim_consumption(claim, fd) is True
    finally:
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert claim.is_dir()
    assert quarantine.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "DO NOT TOUCH"
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
    _write_live(loader, "notify-target", version=1, body="OLD")
    _stage_update(loader, slug, target="auto/notify-target")
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
    # Refuse after the claim, so the refusal restore decides the notification.
    monkeypatch.setattr(loader, "_approve_claimed_update_locked", lambda *_a, **_k: None)

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

    def blocking_first_validation(tree, name, **kwargs):
        nonlocal validation_count
        with count_lock:
            validation_count += 1
            current = validation_count
        if current == 1:
            first_inspection.set()
            assert allow_first.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        else:
            unexpected_second_inspection.set()
        return real_validate(tree, name, **kwargs)

    monkeypatch.setattr(loader, "_claim_pending_update", recording_claim)
    monkeypatch.setattr(
        loader,
        "_validate_and_redact_snapshot",
        blocking_first_validation,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(loader.approve_pending_skill, slug)
        assert first_inspection.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        pending.mkdir()
        (pending / "SKILL.md").write_text("REPLACEMENT", encoding="utf-8")
        (pending / ".meta.json").write_text("{}", encoding="utf-8")
        scripts = pending / "scripts"
        scripts.mkdir()
        (scripts / "late.py").write_text("print('late')\n", encoding="utf-8")
        second = pool.submit(loader.approve_pending_skill, slug)
        assert second_claimed.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        assert not second.done()
        assert not unexpected_second_inspection.is_set()
        allow_first.set()
        assert first.result(timeout=_THREAD_WAIT_CEILING_SECS) == f"auto/{slug}"
        assert second.result(timeout=_THREAD_WAIT_CEILING_SECS) is None

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

    def blocking_rename(source, source_name, destination, destination_name, **kwargs):
        nonlocal blocked
        if (
            source.path == loader._quarantine_root()
            and source_name == claim.name
            and destination_name == slug
            and not blocked
        ):
            blocked = True
            entered_restore.set()
            assert allow_restore.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", blocking_rename)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(loader._restore_claimed_update, claim, fd, slug, snapshot)
        assert entered_restore.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        pending.mkdir()
        (pending / "SKILL.md").write_text("DIRECT REPLACEMENT", encoding="utf-8")
        allow_restore.set()
        restored = future.result(timeout=_THREAD_WAIT_CEILING_SECS)
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    assert restored is not None and restored.name == f"{slug}-2"
    assert (pending / "SKILL.md").read_text(encoding="utf-8") == "DIRECT REPLACEMENT"
    assert (restored / "SKILL.md").read_bytes() == original_skill
    assert (restored / ".meta.json").read_bytes() == original_metadata
    assert not (loader._evidence_root() / claim.name).exists()
    assert not (loader._quarantine_root() / claim.name).exists()
    assert not claim.exists()
    loader._cleanup_claim_lock(claim.name)
    assert not loader._claim_lock_path(claim.name).exists()


def test_claim_refuses_before_public_move_when_no_replace_is_unsupported(
    loader, monkeypatch, caplog
):
    slug = "unsupported-no-replace"
    _stage_update(loader, slug, target="auto/no-replace-target")
    pending = loader._pending_root() / slug

    def unsupported(_source, _source_name, _destination, _destination_name, **_kwargs):
        raise OSError(errno.ENOTSUP, "atomic no-replace rename unavailable")

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", unsupported)

    assert loader._claim_pending_update(slug) is None
    assert pending.is_dir()
    assert "new steps" in (pending / "SKILL.md").read_text(encoding="utf-8")
    assert not list(loader._claims_root().glob(f"{slug}--*"))
    assert not list((loader._locks_root() / "claims").glob(f"{slug}--*.lock"))
    assert "Move KIROCREW_HOME to a local filesystem" in caplog.text
    assert "renameat2(RENAME_NOREPLACE)" in caplog.text


def test_restart_finishes_private_evidence_move_after_durable_state(loader, monkeypatch):
    slug = "evidence-move-recovery"
    loader.stage_skill_candidate(
        slug,
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nNEW",
        provenance=_prov(),
    )
    real_rename = loader._rename_skill_child_no_replace
    refused = False

    def refuse_first_evidence_move(source, source_name, destination, destination_name, **kwargs):
        nonlocal refused
        if destination.path == loader._evidence_root() and not refused:
            refused = True
            raise OSError("injected evidence move interruption")
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", refuse_first_evidence_move)

    assert loader.approve_pending_skill(slug) == f"auto/{slug}"
    claims = list(loader._claims_root().glob(f"{slug}--*"))
    quarantines = list(loader._quarantine_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    assert len(quarantines) == 1
    claim = claims[0]
    lock_path = loader._claim_lock_path(claim.name)
    fd = _open_lock_for_test(loader, lock_path)
    try:
        assert loader._authenticated_claim_evidence_state(fd, lock_path, claim.name) is not None
        assert loader._authenticated_completion_marker(claim) is True
    finally:
        os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert not claim.exists()
    assert (restarted._evidence_root() / claim.name).is_dir()
    assert quarantines[0].is_dir()
    assert lock_path.is_file()


def test_dismissed_candidate_retains_separated_evidence(loader):
    slug = "dismiss-evidence"
    loader.stage_skill_candidate(
        slug,
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nORIGINAL",
        provenance=_prov(),
    )

    assert loader.dismiss_pending_skill(slug) is True

    evidence = list(loader._evidence_root().glob(f"{slug}--*"))
    quarantine = list(loader._quarantine_root().glob(f"{slug}--*"))
    assert len(evidence) == 1
    assert len(quarantine) == 1
    assert loader._authenticated_completion_marker(evidence[0]) is True
    assert loader._claim_lock_path(evidence[0].name).is_file()
    assert loader.list_pending_skills() == []


def test_consumed_quarantine_evidence_releases_slug_for_later_update(loader):
    slug = "retained-evidence-reusable"
    loader.stage_skill_candidate(
        slug,
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nORIGINAL",
        provenance=_prov(),
    )
    assert loader.approve_pending_skill(slug) == f"auto/{slug}"
    assert loader._pending_slug_claimed(slug) is False

    _stage_update(loader, slug, target=f"auto/{slug}", base_version=1)

    assert (loader._pending_root() / slug).is_dir()
    assert not (loader._pending_root() / f"{slug}-2").exists()
    assert loader.approve_pending_update(slug) == f"auto/{slug}"
    assert loader.get_auto_skill_version(f"auto/{slug}") == 2


@pytest.mark.parametrize(
    "active_state",
    ["materialized", "quarantine-only", "linked", "lock-held-evidence", "unjournaled"],
)
def test_active_quarantine_reserves_slug_for_sibling_stage(loader, active_state):
    slug = f"active-quarantine-{active_state}"
    claim = None
    claim_fd = None
    snapshot = None
    if active_state == "unjournaled":
        assert loader._private_state_roots_safe(create=True) is True
        quarantine = loader._quarantine_root() / f"{slug}--0123456789abcdef"
        quarantine.mkdir()
        (quarantine / "SKILL.md").write_text("UNTRUSTED\n", encoding="utf-8")
    else:
        if active_state == "linked":
            _linked_pending_candidate(loader, slug)
        else:
            _stage_update(loader, slug, target="auto/active-target")
        claimed = loader._claim_pending_update(slug)
        assert claimed is not None
        claim, claim_fd, _consumed_at, snapshot = claimed
        quarantine = loader._quarantine_root() / claim.name
        if active_state == "quarantine-only":
            assert loader._discard_trusted_claim(claim) is True
        elif active_state == "lock-held-evidence":
            assert loader._commit_claim_consumption(claim, claim_fd) is True
    quarantine_info = os.lstat(quarantine)

    try:
        staged = loader.stage_skill_candidate(
            slug,
            description="distinct candidate",
            triggers=slug,
            procedure_md="## Steps\n\nDISTINCT",
            provenance=_prov(),
        )
        assert staged == f"auto/{slug}-2"
        assert not (loader._pending_root() / slug).exists()
        assert (loader._pending_root() / f"{slug}-2").is_dir()
        assert os.path.samestat(quarantine_info, os.lstat(quarantine))
    finally:
        if claim is not None and claim_fd is not None and snapshot is not None:
            try:
                if active_state != "lock-held-evidence":
                    assert loader._restore_claimed_update(
                        claim,
                        claim_fd,
                        slug,
                        snapshot,
                    ) == (loader._pending_root() / slug)
            finally:
                skills_mod.platform_compat.release_lock(claim_fd)
                os.close(claim_fd)


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

    def blocking_rename(source, source_name, destination, destination_name, **kwargs):
        if source_name == pending.name:
            entered_rename.set()
            assert allow_rename.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    def claim():
        claim_started.set()
        return loader._claim_pending_update("dismiss-race-update")

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", blocking_rename)
    with ThreadPoolExecutor(max_workers=2) as pool:
        dismissed = pool.submit(loader.dismiss_pending_skill, "dismiss-race-update")
        assert entered_rename.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        claimed = pool.submit(claim)
        assert claim_started.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        assert not claimed.done()
        allow_rename.set()
        assert dismissed.result(timeout=_THREAD_WAIT_CEILING_SECS) is True
        assert claimed.result(timeout=_THREAD_WAIT_CEILING_SECS) is None

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
    real_read_text = path_type.read_text

    def guarded_read_text(path, *args, **kwargs):
        if path == meta_file:
            raise AssertionError("restore reopened trusted candidate metadata")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "read_text", guarded_read_text)

    try:
        loader._restore_claimed_update(claim, fd, "symlinked-meta-update", claim_snapshot)
    finally:
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    assert (loader._pending_root() / "symlinked-meta-update").is_dir()
    assert not claim.exists()
    assert not (loader._evidence_root() / claim.name).exists()
    assert not (loader._quarantine_root() / claim.name).exists()


@pytest.mark.parametrize("flow", ["update", "new", "refusal", "dismiss", "restart"])
@pytest.mark.parametrize("mutation", ["regular-swap", "symlink", "reparse"])
def test_every_flow_isolated_from_public_quarantine_metadata_mutation(
    loader,
    monkeypatch,
    tmp_path,
    flow,
    mutation,
):
    if mutation == "symlink" and os.name == "nt":
        pytest.skip("file-symlink creation needs Windows developer mode")
    slug = f"quarantine-{flow}-{mutation}"
    if flow in {"new", "dismiss"}:
        loader.stage_skill_candidate(
            slug,
            description="original description",
            triggers="original-trigger",
            procedure_md="## Steps\n\nORIGINAL",
            provenance=_prov(),
            notify=False,
        )
        target = slug
        expected_description = "original description"
    else:
        target = f"target-{flow}-{mutation}"
        if flow in {"update", "refusal"}:
            _write_live(loader, target, version=1, body="OLD")
        _stage_update(loader, slug, target=f"auto/{target}", notify=False)
        expected_description = f"updated {slug}"

    secret_file = tmp_path / f"{slug}-secret.json"
    secret_file.write_text('{"secret":"unchanged"}', encoding="utf-8")
    secret_dir = tmp_path / f"{slug}-secret-dir"
    secret_dir.mkdir()
    sentinel = secret_dir / "sentinel"
    sentinel.write_text("DO NOT TOUCH", encoding="utf-8")
    real_rename = loader._rename_skill_child_no_replace
    claim_names: list[str] = []

    def rename_then_mutate(source, source_name, destination, destination_name, **kwargs):
        result = real_rename(source, source_name, destination, destination_name, **kwargs)
        if source_name != slug or destination.path != loader._quarantine_root():
            return result
        claim_names.append(destination_name)
        metadata = destination.path / destination_name / ".meta.json"
        metadata.unlink()
        if mutation == "regular-swap":
            metadata.write_text(
                json.dumps(
                    {
                        "kind": "update",
                        "target": "auto/untrusted-target",
                        "description": "UNTRUSTED LATE MUTATION",
                    }
                ),
                encoding="utf-8",
            )
        elif mutation == "symlink":
            os.symlink(secret_file, metadata)
        else:
            skills_mod.platform_compat.symlink_or_junction(secret_dir, metadata)
        return result

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", rename_then_mutate)

    if flow == "update":
        assert loader.approve_pending_update(slug) == f"auto/{target}"
    elif flow == "new":
        assert loader.approve_pending_skill(slug) == f"auto/{slug}"
    elif flow == "refusal":
        # Refuse AFTER the claim rename, where the restore under test runs; the
        # advisory pre-claim checks would otherwise answer before any claim.
        monkeypatch.setattr(loader, "_approve_claimed_update_locked", lambda *_a, **_k: None)
        assert loader.approve_pending_update(slug) is None
    elif flow == "dismiss":
        assert loader.dismiss_pending_skill(slug) is True
    else:
        claimed = loader._claim_pending_update(slug)
        assert claimed is not None
        _claim, fd, _consumed_at, _snapshot = claimed
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)
        assert [row["slug"] for row in loader.list_pending_skills()] == [slug]

    assert len(claim_names) == 1
    claim_name = claim_names[0]
    if flow in {"update", "new", "dismiss"}:
        quarantine = loader._quarantine_root() / claim_name
        evidence = loader._evidence_root() / claim_name
        assert quarantine.is_dir()
        assert evidence.is_dir()
        trusted_meta = json.loads((evidence / ".meta.json").read_text(encoding="utf-8"))
        assert trusted_meta["description"] == expected_description
        mutated_meta = quarantine / ".meta.json"
    else:
        assert not (loader._quarantine_root() / claim_name).exists()
        assert not (loader._claims_root() / claim_name).exists()
        assert not (loader._evidence_root() / claim_name).exists()
        mutated_meta = loader._pending_root() / slug / ".meta.json"

    if mutation == "regular-swap":
        assert json.loads(mutated_meta.read_text(encoding="utf-8"))["description"] == (
            "UNTRUSTED LATE MUTATION"
        )
    else:
        assert skills_mod.platform_compat.is_link_or_junction(mutated_meta)
    assert secret_file.read_text(encoding="utf-8") == '{"secret":"unchanged"}'
    assert sentinel.read_text(encoding="utf-8") == "DO NOT TOUCH"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permits renaming an opened parent")
@pytest.mark.parametrize(
    "phase",
    ["claim-pending", "claim-quarantine", "restore-pending", "restore-quarantine"],
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

    def swap_then_rename(source, source_name, destination, destination_name, **kwargs):
        should_swap = (
            replacement_kind != "none"
            and not swapped
            and (
                (phase == "claim-pending" and source_name == slug)
                or (phase == "claim-quarantine" and source_name == slug)
                or (
                    phase in {"restore-pending", "restore-quarantine"}
                    and claim is not None
                    and source_name == claim.name
                )
            )
        )
        if should_swap:
            if phase == "claim-pending":
                parent = source.path
            elif phase == "claim-quarantine":
                parent = destination.path
            elif phase == "restore-pending":
                parent = destination.path
            else:
                parent = source.path
            swap_parent(parent)
        return real_rename(source, source_name, destination, destination_name, **kwargs)

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
        elif phase == "claim-quarantine":
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


def test_promoted_abandoned_claim_is_retained_as_separated_evidence(loader):
    _write_live(loader, "promoted-recovery", version=1, body="OLD")
    _stage_update(
        loader,
        "promoted-recovery-update",
        target="auto/promoted-recovery",
    )
    claimed = loader._claim_pending_update("promoted-recovery-update")
    assert claimed is not None
    claim, fd, _consumed_at, _generation = claimed
    quarantine = loader._quarantine_root() / claim.name
    evidence = loader._evidence_root() / claim.name
    assert loader._write_completion_marker(claim) is True
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    assert loader.list_pending_skills() == []
    assert not claim.exists()
    assert quarantine.is_dir()
    assert evidence.is_dir()
    assert loader._claim_lock_path(claim.name).is_file()


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
    real_rename = loader._rename_skill_child_no_replace

    class InjectedCrash(BaseException):
        pass

    def crash_at_generation_publish(source, source_name, destination, destination_name, **kwargs):
        if (
            destination.path == live.parent
            and destination_name == live.name
            and source_name.startswith(".publish-")
        ):
            raise InjectedCrash()
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(
            loader,
            "_rename_skill_child_no_replace",
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
    real_rename = loader._rename_skill_child_no_replace

    class InjectedCrash(BaseException):
        pass

    def crash_before_new_publish(source, source_name, destination, destination_name, **kwargs):
        if source_name.startswith(".publish-") and (
            destination.path == live_dir.parent and destination_name == live_dir.name
        ):
            raise InjectedCrash()
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(
            loader,
            "_rename_skill_child_no_replace",
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
    real_rename = loader._rename_skill_child_no_replace

    class InjectedCrash(BaseException):
        pass

    def crash_before_new_publish(source, source_name, destination, destination_name, **kwargs):
        if source_name.startswith(".publish-") and (
            destination.path == live_dir.parent and destination_name == live_dir.name
        ):
            raise InjectedCrash()
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(
            loader,
            "_rename_skill_child_no_replace",
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
    """A published update is retained as evidence, never re-queued or reapplied."""
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
    evidence = list(restarted._evidence_root().glob(f"{slug}--*"))
    assert len(evidence) == 1
    _stage, retained = restarted._publication_paths(evidence[0].name)
    assert "OLD" in (retained / "SKILL.md").read_text(encoding="utf-8")
    assert restarted._claim_lock_path(evidence[0].name).is_file()


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
    real_hash = restarted._skill_tree_hash_child
    edit_landed = False

    def hash_then_edit(parent, name):
        nonlocal edit_landed
        result = real_hash(parent, name)
        if parent.path == live_dir.parent and name == target and not edit_landed:
            marker.write_text("after hash\n", encoding="utf-8")
            edit_landed = True
        return result

    monkeypatch.setattr(restarted, "_skill_tree_hash_child", hash_then_edit)

    assert restarted.list_pending_skills() == []
    assert edit_landed is True
    assert marker.read_text(encoding="utf-8") == "after hash\n"
    assert claims[0].is_dir()
    assert restarted._skill_tree_hash(backup) == before_hash


def test_recovery_no_replace_preserves_concurrent_recreated_live_target(loader, monkeypatch):
    target = "recovery-recreated-live"
    slug = f"{target}-update"
    live_dir = _write_live(loader, target, version=1, body="OLD")
    before_hash = loader._skill_tree_hash(live_dir)
    _stage_update(loader, slug, target=f"auto/{target}")
    real_rename = loader._rename_skill_child_no_replace

    class InjectedCrash(BaseException):
        pass

    def crash_before_stage_publish(source, source_name, destination, destination_name, **kwargs):
        if source_name.startswith(".publish-") and (
            destination.path == live_dir.parent and destination_name == live_dir.name
        ):
            raise InjectedCrash()
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(
            loader,
            "_rename_skill_child_no_replace",
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
    real_restarted_rename = restarted._rename_skill_child_no_replace
    writer_landed = False

    def writer_wins_restore(source, source_name, destination, destination_name, **kwargs):
        nonlocal writer_landed
        if (
            source.path == backup.parent
            and source_name == backup.name
            and destination.path == live_dir.parent
            and destination_name == live_dir.name
            and not writer_landed
        ):
            _write_live(loader, target, version=7, body="CONCURRENT")
            writer_landed = True
        return real_restarted_rename(source, source_name, destination, destination_name)

    monkeypatch.setattr(
        restarted,
        "_rename_skill_child_no_replace",
        writer_wins_restore,
    )

    assert restarted.list_pending_skills() == []
    assert writer_landed is True
    assert "CONCURRENT" in (live_dir / "SKILL.md").read_text(encoding="utf-8")
    assert restarted.get_auto_skill_version(f"auto/{target}") == 7
    assert claims[0].is_dir()
    assert restarted._skill_tree_hash(stage) is not None
    assert restarted._skill_tree_hash(backup) == before_hash


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX permits a writable descriptor to survive the live-tree rename",
)
def test_in_process_rollback_retains_late_backup_write_after_recovery_hash(
    loader,
    monkeypatch,
):
    target = "rollback-late-backup-write"
    slug = f"{target}-update"
    live_dir = _write_live(loader, target, version=1, body="OLD")
    retained_fd = os.open(live_dir / "SKILL.md", os.O_WRONLY)
    _stage_update(loader, slug, target=f"auto/{target}")
    real_rename = loader._rename_skill_child_no_replace
    real_hash = loader._skill_tree_hash_child
    recreated_live = False
    backup_path: Path | None = None
    backup_hashes = 0
    late_generation = b"LATE RETAINED BACKUP WRITE\n"

    def recreate_before_generation(source, source_name, destination, destination_name, **kwargs):
        nonlocal recreated_live, backup_path
        if (
            source_name.startswith(".publish-")
            and destination.path == live_dir.parent
            and destination_name == live_dir.name
            and not recreated_live
        ):
            backup_path = loader._live_quarantine_root() / source_name.removeprefix(".publish-")
            shutil.copytree(
                backup_path, destination.path / destination_name, copy_function=shutil.copy2
            )
            recreated_live = True
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    def hash_then_write_through_retained_descriptor(parent, name):
        nonlocal backup_hashes
        result = real_hash(parent, name)
        if parent.path == loader._live_quarantine_root():
            backup_hashes += 1
            if backup_hashes == 2:
                os.lseek(retained_fd, 0, os.SEEK_SET)
                os.ftruncate(retained_fd, 0)
                os.write(retained_fd, late_generation)
                os.fsync(retained_fd)
        return result

    monkeypatch.setattr(
        loader,
        "_rename_skill_child_no_replace",
        recreate_before_generation,
    )
    monkeypatch.setattr(
        loader,
        "_skill_tree_hash_child",
        hash_then_write_through_retained_descriptor,
    )
    try:
        assert loader.approve_pending_update(slug) is None
    finally:
        os.close(retained_fd)

    assert recreated_live is True
    assert backup_hashes == 2
    assert backup_path is not None
    claim_name = backup_path.name
    claim = loader._claims_root() / claim_name
    stage, backup = loader._publication_paths(claim_name)
    quarantine = loader._quarantine_root() / claim_name
    lock_path = loader._claim_lock_path(claim_name)
    assert backup == backup_path
    assert (backup / "SKILL.md").read_bytes() == late_generation
    assert claim.is_dir()
    assert stage.is_dir()
    assert quarantine.is_dir()
    assert lock_path.is_file()
    assert not (loader._pending_root() / slug).exists()
    journal_fd = _open_lock_for_test(loader, lock_path)
    try:
        journal = loader._authenticated_claim_publication(
            journal_fd,
            lock_path,
            claim_name,
        )
    finally:
        os.close(journal_fd)
    assert journal is not None and journal["state"] == "prepared"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX permits a writable descriptor to survive a gateway restart",
)
def test_restart_rollback_retains_late_backup_write_after_recovery_hash(
    loader,
    monkeypatch,
):
    target = "restart-late-backup-write"
    slug = f"{target}-update"
    live_dir = _write_live(loader, target, version=1, body="OLD")
    retained_fd = os.open(live_dir / "SKILL.md", os.O_WRONLY)
    _stage_update(loader, slug, target=f"auto/{target}")
    real_rename = loader._rename_skill_child_no_replace
    backup_path: Path | None = None
    recreated_live = False

    def recreate_before_generation(source, source_name, destination, destination_name, **kwargs):
        nonlocal backup_path, recreated_live
        if (
            source_name.startswith(".publish-")
            and destination.path == live_dir.parent
            and destination_name == live_dir.name
            and not recreated_live
        ):
            backup_path = loader._live_quarantine_root() / source_name.removeprefix(".publish-")
            shutil.copytree(
                backup_path, destination.path / destination_name, copy_function=shutil.copy2
            )
            recreated_live = True
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    with monkeypatch.context() as initial_process:
        initial_process.setattr(
            loader,
            "_rename_skill_child_no_replace",
            recreate_before_generation,
        )
        initial_process.setattr(
            loader,
            "_restore_failed_promotion_claim",
            lambda *_args: False,
        )
        assert loader.approve_pending_update(slug) is None

    assert recreated_live is True
    assert backup_path is not None
    claim_name = backup_path.name
    claim = loader._claims_root() / claim_name
    stage, backup = loader._publication_paths(claim_name)
    quarantine = loader._quarantine_root() / claim_name
    lock_path = loader._claim_lock_path(claim_name)
    assert backup == backup_path
    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    real_restarted_hash = restarted._skill_tree_hash_child
    wrote_late_generation = False
    late_generation = b"RESTART RETAINED BACKUP WRITE\n"

    def hash_then_write_through_retained_descriptor(parent, name):
        nonlocal wrote_late_generation
        result = real_restarted_hash(parent, name)
        if parent.path == backup.parent and name == backup.name and not wrote_late_generation:
            os.lseek(retained_fd, 0, os.SEEK_SET)
            os.ftruncate(retained_fd, 0)
            os.write(retained_fd, late_generation)
            os.fsync(retained_fd)
            wrote_late_generation = True
        return result

    monkeypatch.setattr(
        restarted,
        "_skill_tree_hash_child",
        hash_then_write_through_retained_descriptor,
    )
    try:
        pending = restarted.list_pending_skills()
    finally:
        os.close(retained_fd)

    assert pending == []
    assert wrote_late_generation is True
    assert (backup / "SKILL.md").read_bytes() == late_generation
    assert claim.is_dir()
    assert stage.is_dir()
    assert quarantine.is_dir()
    assert lock_path.is_file()
    assert not (restarted._pending_root() / slug).exists()
    journal_fd = _open_lock_for_test(restarted, lock_path)
    try:
        journal = restarted._authenticated_claim_publication(
            journal_fd,
            lock_path,
            claim_name,
        )
    finally:
        os.close(journal_fd)
    assert journal is not None and journal["state"] == "prepared"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX permits a writable descriptor to survive the live-tree rename",
)
@pytest.mark.parametrize("mutation", ["edit", "replace"])
def test_post_publish_backup_drift_preserves_newer_live_and_ambiguous_artifacts(
    loader,
    monkeypatch,
    mutation,
):
    target = f"combined-backup-live-{mutation}"
    slug = f"{target}-update"
    live_dir = _write_live(loader, target, version=1, body="OLD")
    retained_fd = os.open(live_dir / "SKILL.md", os.O_WRONLY)
    _stage_update(loader, slug, target=f"auto/{target}")
    real_hash = loader._skill_tree_hash_child
    backup_hashes = 0
    race_landed = False
    late_backup = b"LATE BACKUP GENERATION\n"
    detached_live = live_dir.with_name(f"{target}-detached-after")

    def race_before_second_backup_hash(parent, name):
        nonlocal backup_hashes, race_landed
        if parent.path == loader._live_quarantine_root():
            backup_hashes += 1
            if backup_hashes == 2:
                os.lseek(retained_fd, 0, os.SEEK_SET)
                os.ftruncate(retained_fd, 0)
                os.write(retained_fd, late_backup)
                os.fsync(retained_fd)
                if mutation == "edit":
                    (live_dir / "SKILL.md").write_text(
                        "CONCURRENT LIVE EDIT\n",
                        encoding="utf-8",
                    )
                else:
                    live_dir.rename(detached_live)
                    _write_live(loader, target, version=7, body="CONCURRENT LIVE REPLACEMENT")
                race_landed = True
        return real_hash(parent, name)

    monkeypatch.setattr(
        loader,
        "_skill_tree_hash_child",
        race_before_second_backup_hash,
    )
    try:
        assert loader.approve_pending_update(slug) is None
    finally:
        os.close(retained_fd)

    assert race_landed is True
    expected_live = "EDIT" if mutation == "edit" else "REPLACEMENT"
    assert expected_live in (live_dir / "SKILL.md").read_text(encoding="utf-8")
    if mutation == "replace":
        assert "new steps" in (detached_live / "SKILL.md").read_text(encoding="utf-8")

    claims = list(loader._claims_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    claim = claims[0]
    stage, backup = loader._publication_paths(claim.name)
    quarantine = loader._quarantine_root() / claim.name
    lock_path = loader._claim_lock_path(claim.name)
    assert not stage.exists()
    assert (backup / "SKILL.md").read_bytes() == late_backup
    assert claim.is_dir()
    assert quarantine.is_dir()
    assert lock_path.is_file()
    assert not (loader._pending_root() / slug).exists()

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert expected_live in (live_dir / "SKILL.md").read_text(encoding="utf-8")
    assert (backup / "SKILL.md").read_bytes() == late_backup
    assert claim.is_dir()
    assert quarantine.is_dir()
    assert lock_path.is_file()
    if mutation == "replace":
        assert "new steps" in (detached_live / "SKILL.md").read_text(encoding="utf-8")


# ── lifecycle namespace durability ordering ──


def test_create_capable_authority_children_sync_parent_before_yield(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    """Every authority name is durable before a caller can mutate through it."""
    real_pin_child = loader._pin_skill_child_parent
    real_sync = loader._sync_pinned_parent
    synced: list[Path] = []
    observed: list[tuple[Path, Path]] = []

    def recording_sync(parent):
        synced.append(parent.path)
        return real_sync(parent)

    @contextlib.contextmanager
    def asserting_pin(parent, name, *, create, created_out=None):
        with real_pin_child(
            parent,
            name,
            create=create,
            created_out=created_out,
        ) as child:
            if create:
                assert synced and synced[-1] == parent.path
                observed.append((child.path, parent.path))
            yield child

    monkeypatch.setattr(loader, "_sync_pinned_parent", recording_sync)
    monkeypatch.setattr(loader, "_pin_skill_child_parent", asserting_pin)

    def assert_complete_authority_hierarchy(*, creates_private_root: bool) -> None:
        observed.clear()
        synced.clear()
        if creates_private_root:
            home = loader._private_root().parents[1]
            binding = loader._ensure_private_authority(
                home,
                configured_home=home,
                create=True,
            )
            with skills_mod._AUTHORITY_HOME_IDENTITIES_LOCK:
                skills_mod._STARTUP_AUTHORITY_BINDING = binding
        with loader._pin_private_state(create=True) as state:
            expected = {
                (state.auto.path, state.auto.path.parent),
                (state.pending.path, state.auto.path),
                (state.quarantine.path, state.auto.path),
                (state.live_quarantine.path, state.auto.path),
                (state.claims.path, state.private.path),
                (state.evidence.path, state.private.path),
                (state.locks.path, state.private.path),
                (state.claim_locks.path, state.locks.path),
            }
            if creates_private_root:
                expected.update(
                    {
                        (
                            state.private.path,
                            state.data_home.path / skills_mod._AUTHORITY_PROVENANCE_PARENT,
                        ),
                        (
                            state.data_home.path / skills_mod._AUTHORITY_PROVENANCE_PARENT,
                            state.data_home.path,
                        ),
                    }
                )
            assert set(observed) == expected

    # First pass creates every authority child. The second proves an existing
    # child on a create-capable path repairs an interrupted earlier creation.
    assert_complete_authority_hierarchy(creates_private_root=True)
    assert_complete_authority_hierarchy(creates_private_root=False)


def test_claim_lock_ancestor_sync_failure_precedes_public_mutation(loader, monkeypatch):
    target = "claim-lock-ancestor-sync-target"
    slug = f"{target}-update"
    live = _write_live(loader, target, version=1, body="ORIGINAL")
    _stage_update(loader, slug, target=f"auto/{target}")
    pending = loader._pending_root() / slug
    claim_locks = loader._locks_root() / skills_mod.AUTO_CLAIMS_DIRNAME
    claim_locks.rmdir()
    real_sync = loader._sync_pinned_parent

    def fail_claim_locks_parent(parent):
        if parent.path == loader._locks_root():
            raise OSError("injected claim-lock ancestor sync failure")
        return real_sync(parent)

    def forbid_rename(*_args, **_kwargs):
        pytest.fail("authority rename ran before the claim-lock ancestor was durable")

    def forbid_materialization(*_args, **_kwargs):
        pytest.fail("candidate materialization ran before the claim-lock ancestor was durable")

    monkeypatch.setattr(loader, "_sync_pinned_parent", fail_claim_locks_parent)
    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", forbid_rename)
    monkeypatch.setattr(loader, "_materialize_skill_tree_snapshot", forbid_materialization)

    assert loader._claim_pending_update(slug) is None
    assert pending.is_dir()
    assert "ORIGINAL" in (live / "SKILL.md").read_text(encoding="utf-8")
    assert not list(loader._quarantine_root().glob(f"{slug}--*"))
    assert not list(loader._claims_root().glob(f"{slug}--*"))


def test_claim_lock_and_quarantine_parents_sync_before_materialization(loader, monkeypatch):
    slug = "claim-namespace-sync-order"
    _stage_update(loader, slug, target="auto/claim-namespace-target")
    pending_parent = loader._pending_root()
    quarantine_parent = loader._quarantine_root()
    claim_locks_parent = loader._locks_root() / "claims"
    events: list[tuple[str, str]] = []
    real_sync = loader._sync_pinned_parent
    real_rename = loader._rename_skill_child_no_replace
    real_materialize = loader._materialize_skill_tree_snapshot

    def recording_sync(parent):
        if parent.path in {pending_parent, quarantine_parent, claim_locks_parent}:
            events.append(("sync", str(parent.path)))
        return real_sync(parent)

    def recording_rename(source, source_name, destination, destination_name, **kwargs):
        if source.path == pending_parent and destination.path == quarantine_parent:
            events.append(("rename", "pending->quarantine"))
            assert ("sync", str(claim_locks_parent)) in events
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    def recording_materialize(snapshot, destination):
        events.append(("authority", "materialize"))
        return real_materialize(snapshot, destination)

    monkeypatch.setattr(loader, "_sync_pinned_parent", recording_sync)
    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", recording_rename)
    monkeypatch.setattr(loader, "_materialize_skill_tree_snapshot", recording_materialize)

    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, snapshot = claimed
    try:
        renamed = events.index(("rename", "pending->quarantine"))
        materialized = events.index(("authority", "materialize"))
        assert renamed < events.index(("sync", str(quarantine_parent))) < materialized
        assert renamed < events.index(("sync", str(pending_parent))) < materialized
    finally:
        loader._restore_claimed_update(claim, fd, slug, snapshot)
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)


def test_claim_lock_parent_sync_failure_cleans_before_public_mutation(loader, monkeypatch):
    slug = "claim-lock-sync-failure"
    _stage_update(loader, slug, target="auto/claim-lock-sync-target")
    pending = loader._pending_root() / slug
    claim_locks_parent = loader._locks_root() / "claims"
    real_sync = loader._sync_pinned_parent
    real_rename = loader._rename_skill_child_no_replace

    def fail_claim_lock_parent(parent):
        if parent.path == claim_locks_parent:
            raise OSError("injected claim-lock parent sync failure")
        return real_sync(parent)

    def refuse_public_move(source, source_name, destination, destination_name, **kwargs):
        if source.path == loader._pending_root() and destination.path == loader._quarantine_root():
            pytest.fail("public candidate moved before its fresh claim-lock name was durable")
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    monkeypatch.setattr(loader, "_sync_pinned_parent", fail_claim_lock_parent)
    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", refuse_public_move)

    assert loader._claim_pending_update(slug) is None
    assert pending.is_dir()
    assert not list(claim_locks_parent.glob(f"{slug}--*.lock"))
    assert not list(loader._quarantine_root().glob(f"{slug}--*"))


def test_existing_skill_lock_is_not_reported_as_fresh(loader):
    assert loader._private_state_roots_safe(create=True) is True
    lock_name = "existing-claim.lock"
    lock_path = loader._locks_root() / "claims" / lock_name
    lock_path.write_bytes(b"existing")

    with loader._pin_private_state(create=False) as state:
        created: list[bool] = []
        fd = loader._open_skill_lock(
            state.claim_locks,
            lock_name,
            created_out=created,
        )
    os.close(fd)

    assert created == [False]
    assert lock_path.read_bytes() == b"existing"


def test_forward_publication_syncs_both_parents_before_each_authority_step(loader, monkeypatch):
    assert loader._private_state_roots_safe(create=True) is True
    live = _write_live(loader, "forward-sync-order", version=1, body="BEFORE")
    stage, backup = loader._publication_paths("forward-sync-order--claim")
    shutil.copytree(live, stage)
    staged_skill = stage / "SKILL.md"
    staged_skill.write_text(
        staged_skill.read_text(encoding="utf-8").replace("BEFORE", "AFTER"),
        encoding="utf-8",
    )
    before_hash = loader._skill_tree_hash(live)
    after_hash = loader._skill_tree_hash(stage)
    backup_identity = _tagged_identity_for_path(loader, live)
    assert before_hash is not None and after_hash is not None

    events: list[tuple[str, str]] = []
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        real_rename = loader._rename_skill_child_no_replace
        real_sync = loader._sync_pinned_parent
        real_hash = loader._skill_tree_hash_child

        def recording_rename(source, source_name, destination, destination_name, **kwargs):
            events.append(("rename", f"{source_name}->{destination_name}"))
            return real_rename(source, source_name, destination, destination_name, **kwargs)

        def recording_sync(parent):
            events.append(("sync", str(parent.path)))
            return real_sync(parent)

        def recording_hash(parent, name):
            if (
                parent.native_identity == state.live_quarantine.native_identity
                and name == backup.name
            ):
                events.append(("authority", "hash-backup"))
            return real_hash(parent, name)

        monkeypatch.setattr(loader, "_rename_skill_child_no_replace", recording_rename)
        monkeypatch.setattr(loader, "_sync_pinned_parent", recording_sync)
        monkeypatch.setattr(loader, "_skill_tree_hash_child", recording_hash)

        assert (
            loader._publish_prepared_skill_tree(
                state=state,
                live_dir=live,
                stage=stage,
                backup=backup,
                backup_identity=backup_identity,
                before_hash=before_hash,
                after_hash=after_hash,
            )
            == "published"
        )

    live_to_backup = events.index(("rename", f"{live.name}->{backup.name}"))
    first_backup_hash = events.index(("authority", "hash-backup"))
    stage_to_live = events.index(("rename", f"{stage.name}->{live.name}"))
    assert live_to_backup < events.index(("sync", str(backup.parent))) < first_backup_hash
    assert live_to_backup < events.index(("sync", str(live.parent))) < first_backup_hash
    assert first_backup_hash < stage_to_live
    second_backup_hash = next(
        index
        for index in range(first_backup_hash + 1, len(events))
        if events[index] == ("authority", "hash-backup")
    )
    assert events.index(("sync", str(live.parent)), stage_to_live + 1) < second_backup_hash
    assert events.index(("sync", str(stage.parent)), stage_to_live + 1) < second_backup_hash


@pytest.mark.skipif(os.name == "nt", reason="Windows parent handles prohibit the injected rename")
def test_publication_refuses_transient_auto_parent_replacement(loader, monkeypatch):
    assert loader._private_state_roots_safe(create=True) is True
    live = _write_live(loader, "publication-parent-swap", version=1, body="BEFORE")
    stage, backup = loader._publication_paths("publication-parent-swap--claim")
    shutil.copytree(live, stage)
    staged_skill = stage / "SKILL.md"
    staged_skill.write_text(
        staged_skill.read_text(encoding="utf-8").replace("BEFORE", "AFTER"),
        encoding="utf-8",
    )
    before_hash = loader._skill_tree_hash(live)
    after_hash = loader._skill_tree_hash(stage)
    backup_identity = _tagged_identity_for_path(loader, live)
    assert before_hash is not None and after_hash is not None
    auto = live.parent
    detached = auto.with_name("auto-detached-during-publication")

    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        real_rename = loader._rename_skill_child_no_replace
        swapped = False

        def replace_restore_auto(source, source_name, destination, destination_name, **kwargs):
            nonlocal swapped
            if source.identity == state.auto.identity and source_name == live.name and not swapped:
                auto.rename(detached)
                auto.mkdir()
                shutil.copytree(detached / live.name, auto / live.name)
                swapped = True
                try:
                    return real_rename(source, source_name, destination, destination_name, **kwargs)
                finally:
                    shutil.rmtree(auto)
                    detached.rename(auto)
            return real_rename(source, source_name, destination, destination_name, **kwargs)

        monkeypatch.setattr(loader, "_rename_skill_child_no_replace", replace_restore_auto)
        outcome = loader._publish_prepared_skill_tree(
            state=state,
            live_dir=live,
            stage=stage,
            backup=backup,
            backup_identity=backup_identity,
            before_hash=before_hash,
            after_hash=after_hash,
        )

    assert swapped is True
    assert outcome == "incomplete"
    assert "BEFORE" in (live / "SKILL.md").read_text(encoding="utf-8")
    assert "AFTER" in (stage / "SKILL.md").read_text(encoding="utf-8")
    assert not backup.exists()
    assert not detached.exists()


def test_post_publish_backup_drift_keeps_committed_live_without_reverse_rename(loader, monkeypatch):
    assert loader._private_state_roots_safe(create=True) is True
    live = _write_live(loader, "drift-sync-order", version=1, body="BEFORE")
    stage, backup = loader._publication_paths("drift-sync-order--claim")
    shutil.copytree(live, stage)
    staged_skill = stage / "SKILL.md"
    staged_skill.write_text(
        staged_skill.read_text(encoding="utf-8").replace("BEFORE", "AFTER"),
        encoding="utf-8",
    )
    before_hash = loader._skill_tree_hash(live)
    after_hash = loader._skill_tree_hash(stage)
    backup_identity = _tagged_identity_for_path(loader, live)
    assert before_hash is not None and after_hash is not None

    events: list[tuple[str, str]] = []
    backup_hashes = 0
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        real_rename = loader._rename_skill_child_no_replace
        real_hash = loader._skill_tree_hash_child

        def recording_rename(source, source_name, destination, destination_name, **kwargs):
            events.append(("rename", f"{source_name}->{destination_name}"))
            return real_rename(source, source_name, destination, destination_name, **kwargs)

        def force_second_backup_hash_to_drift(parent, name):
            nonlocal backup_hashes
            result = real_hash(parent, name)
            if (
                parent.native_identity == state.live_quarantine.native_identity
                and name == backup.name
            ):
                backup_hashes += 1
                if backup_hashes == 2:
                    return "0" * 64
            return result

        monkeypatch.setattr(loader, "_rename_skill_child_no_replace", recording_rename)
        monkeypatch.setattr(
            loader,
            "_skill_tree_hash_child",
            force_second_backup_hash_to_drift,
        )

        assert (
            loader._publish_prepared_skill_tree(
                state=state,
                live_dir=live,
                stage=stage,
                backup=backup,
                backup_identity=backup_identity,
                before_hash=before_hash,
                after_hash=after_hash,
            )
            == "published"
        )

    assert "AFTER" in (live / "SKILL.md").read_text(encoding="utf-8")
    assert backup.is_dir()
    assert not stage.exists()
    assert events == [
        ("rename", f"{live.name}->{backup.name}"),
        ("rename", f"{stage.name}->{live.name}"),
    ]


def test_reconcile_backup_restore_syncs_both_parents_before_classification(loader, monkeypatch):
    assert loader._private_state_roots_safe(create=True) is True
    target = "reconcile-sync-order"
    live = _write_live(loader, target, version=1, body="BEFORE")
    claim_name = "reconcile-sync-order--claim"
    claim = loader._claims_root() / claim_name
    claim.mkdir()
    stage, backup = loader._publication_paths(claim_name)
    before_hash = loader._skill_tree_hash(live)
    backup_identity = _tagged_identity_for_path(loader, live)
    shutil.copytree(live, stage)
    staged_skill = stage / "SKILL.md"
    staged_skill.write_text(
        staged_skill.read_text(encoding="utf-8").replace("BEFORE", "AFTER"),
        encoding="utf-8",
    )
    after_hash = loader._skill_tree_hash(stage)
    assert before_hash is not None and after_hash is not None

    journal = {
        "state": "prepared",
        "format": 2,
        "claim": claim_name,
        "kind": "update",
        "target": target,
        "before": before_hash,
        "after": after_hash,
        "live_backup_identity": loader._identity_payload(backup_identity),
        "snapshot": 1,
        "version": 2,
    }
    events: list[tuple[str, str]] = []
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        loader._rename_skill_child_no_replace(
            state.auto,
            target,
            state.live_quarantine,
            backup.name,
            expected_identity=backup_identity,
        )
        real_rename = loader._rename_skill_child_no_replace
        real_sync = loader._sync_pinned_parent
        real_hash = loader._skill_tree_hash_child

        def recording_rename(source, source_name, destination, destination_name, **kwargs):
            events.append(("rename", f"{source_name}->{destination_name}"))
            return real_rename(source, source_name, destination, destination_name, **kwargs)

        def recording_sync(parent):
            events.append(("sync", str(parent.path)))
            return real_sync(parent)

        def recording_hash(parent, name):
            result = real_hash(parent, name)
            if parent.identity == state.auto.identity and name == target:
                events.append(("authority", "classify-live"))
            return result

        monkeypatch.setattr(loader, "_authenticated_claim_publication", lambda *_args: journal)
        monkeypatch.setattr(loader, "_rename_skill_child_no_replace", recording_rename)
        monkeypatch.setattr(loader, "_sync_pinned_parent", recording_sync)
        monkeypatch.setattr(loader, "_skill_tree_hash_child", recording_hash)

        assert (
            loader._reconcile_prepared_claim(
                claim,
                -1,
                loader._claim_lock_path(claim_name),
                private_state=state,
            )
            is False
        )

    restored = events.index(("rename", f"{backup.name}->{live.name}"))
    classified = events.index(("authority", "classify-live"))
    assert restored < events.index(("sync", str(live.parent)), restored + 1) < classified
    assert restored < events.index(("sync", str(backup.parent)), restored + 1) < classified


@pytest.mark.skipif(os.name == "nt", reason="POSIX rename destination-swap injection")
def test_private_publish_destination_swap_leaves_newer_live_public(loader, monkeypatch):
    target = "post-rename-destination-swap"
    claim_name = f"{target}--claim"
    stage, _backup = loader._publication_paths(claim_name)
    stage.mkdir()
    (stage / "SKILL.md").write_text("AUTHENTIC AFTER", encoding="utf-8")
    after_hash = loader._skill_tree_hash(stage)
    assert after_hash is not None
    live = loader._dir / "auto" / target
    displaced = loader._dir / "auto" / f"{target}-authenticated-displaced"
    real_rename = skills_mod.platform_compat.rename_noreplace
    swapped = False

    def rename_then_swap_destination(src, dst, *, src_dir_fd, dst_dir_fd):
        nonlocal swapped
        result = real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if src == stage.name and dst == target and not swapped:
            os.rename(dst, displaced.name, src_dir_fd=dst_dir_fd, dst_dir_fd=dst_dir_fd)
            live.mkdir()
            (live / "SKILL.md").write_text("NEWER PUBLIC LIVE", encoding="utf-8")
            swapped = True
        return result

    monkeypatch.setattr(
        skills_mod.platform_compat,
        "rename_noreplace",
        rename_then_swap_destination,
    )
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        outcome = loader._publish_prepared_skill_tree(
            state=state,
            live_dir=live,
            stage=stage,
            backup=None,
            backup_identity=None,
            before_hash=None,
            after_hash=after_hash,
        )

    assert swapped is True
    assert outcome == "incomplete"
    assert (live / "SKILL.md").read_text(encoding="utf-8") == "NEWER PUBLIC LIVE"
    assert (displaced / "SKILL.md").read_text(encoding="utf-8") == "AUTHENTIC AFTER"
    assert not stage.exists()
    assert live.parent == loader._dir / "auto"
    assert displaced.parent == loader._dir / "auto"


@pytest.mark.skipif(os.name == "nt", reason="POSIX rename source-swap injection")
def test_forward_source_swap_never_evacuates_replacement_live(loader, monkeypatch):
    live = _write_live(loader, "forward-source-swap", version=1, body="ORIGINAL")
    stage, backup = loader._publication_paths("forward-source-swap--claim")
    shutil.copytree(live, stage)
    (stage / "SKILL.md").write_text("AFTER", encoding="utf-8")
    before_hash = loader._skill_tree_hash(live)
    after_hash = loader._skill_tree_hash(stage)
    expected_identity = _tagged_identity_for_path(loader, live)
    original_name = "forward-source-swap-original"
    real_rename = skills_mod.platform_compat.rename_noreplace
    swapped = False

    def swap_source_then_rename(src, dst, *, src_dir_fd, dst_dir_fd):
        nonlocal swapped
        if src == live.name and dst == backup.name and not swapped:
            os.rename(
                src,
                original_name,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=src_dir_fd,
            )
            shutil.copytree(live.parent / original_name, live)
            (live / "SKILL.md").write_text("CONCURRENT LIVE", encoding="utf-8")
            swapped = True
        return real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(skills_mod.platform_compat, "rename_noreplace", swap_source_then_rename)
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        outcome = loader._publish_prepared_skill_tree(
            state=state,
            live_dir=live,
            stage=stage,
            backup=backup,
            backup_identity=expected_identity,
            before_hash=before_hash,
            after_hash=after_hash,
        )

    assert swapped is True
    assert outcome == "incomplete"
    assert (live / "SKILL.md").read_text(encoding="utf-8") == "CONCURRENT LIVE"
    assert "ORIGINAL" in (live.parent / original_name / "SKILL.md").read_text(encoding="utf-8")
    assert not backup.exists()
    assert stage.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX rename source-swap injection")
def test_rollback_source_swap_never_leaves_replacement_live_on_restart(loader, monkeypatch):
    target = "rollback-source-swap"
    live = _write_live(loader, target, version=1, body="BEFORE")
    stage, backup = loader._publication_paths("rollback-source-swap--claim")
    shutil.copytree(live, stage)
    (stage / "SKILL.md").write_text("AFTER", encoding="utf-8")
    before_hash = loader._skill_tree_hash(live)
    after_hash = loader._skill_tree_hash(stage)
    backup_identity = _tagged_identity_for_path(loader, live)
    claim_name = backup.name
    journal = {
        "state": "prepared",
        "format": 2,
        "claim": claim_name,
        "kind": "update",
        "target": target,
        "before": before_hash,
        "after": after_hash,
        "live_backup_identity": loader._identity_payload(backup_identity),
        "snapshot": 1,
        "version": 2,
    }
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        loader._rename_skill_child_no_replace(
            state.auto,
            target,
            state.live_quarantine,
            backup.name,
            expected_identity=backup_identity,
        )
    original_name = f"{backup.name}-original"
    real_rename = skills_mod.platform_compat.rename_noreplace
    swapped = False

    def swap_source_then_rename(src, dst, *, src_dir_fd, dst_dir_fd):
        nonlocal swapped
        if src == backup.name and dst == target and not swapped:
            os.rename(
                src,
                original_name,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=src_dir_fd,
            )
            shutil.copytree(backup.parent / original_name, backup)
            (backup / "SKILL.md").write_text("REPLACEMENT EVIDENCE", encoding="utf-8")
            swapped = True
        return real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(skills_mod.platform_compat, "rename_noreplace", swap_source_then_rename)
    monkeypatch.setattr(loader, "_authenticated_claim_publication", lambda *_args: journal)
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        assert loader._reconcile_prepared_claim_pinned(journal, state, claim_name) is None
        assert loader._reconcile_prepared_claim_pinned(journal, state, claim_name) is None

    assert swapped is True
    assert not live.exists()
    assert "REPLACEMENT EVIDENCE" in (backup / "SKILL.md").read_text(encoding="utf-8")
    assert "BEFORE" in (backup.parent / original_name / "SKILL.md").read_text(encoding="utf-8")
    assert stage.is_dir()


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

    def unexpected_delete(*_args, **_kwargs):
        pytest.fail("reserved auto namespace reached the recursive delete helper")

    monkeypatch.setattr(skills_mod, "sel", lambda: AuditSink())
    monkeypatch.setattr(shutil, "rmtree", unexpected_delete)

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
        """A DirEntry whose stat() zeroes st_ino/st_dev/st_nlink like Windows.

        Everything else is the real entry's: the patch is live for every scandir
        caller until teardown, and shutil.rmtree's Windows walk asks is_junction().
        """

        def __init__(self, real):
            self._real = real
            self.name = real.name
            self.path = real.path

        def __getattr__(self, name):
            return getattr(self._real, name)

        def is_symlink(self):
            return self._real.is_symlink()

        def is_dir(self, *, follow_symlinks=True):
            return self._real.is_dir(follow_symlinks=follow_symlinks)

        def stat(self, *, follow_symlinks=True):
            real = self._real.stat(follow_symlinks=follow_symlinks)
            return _restat(real, st_ino=0, st_dev=0, st_nlink=0)

    with real_scandir(root) as entries:
        stand_in = _WinEntry(next(iter(entries)))
    assert all(hasattr(stand_in, name) for name in dir(os.DirEntry) if not name.startswith("_"))

    def windows_scandir(path):
        inner = real_scandir(path)

        class _Scanner:
            def __enter__(self):
                inner.__enter__()
                return self

            def __exit__(self, *exc):
                return inner.__exit__(*exc)

            def __iter__(self):
                return self

            def __next__(self):
                return _WinEntry(next(inner))

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

    # The snapshot every claim and preview captures refuses the whole tree, so
    # approval fails closed and leaves the candidate queued.
    assert loader._skill_tree_snapshot(loader._pending_root() / slug) is None
    assert loader.approve_pending_skill(slug) is None
    assert skill_file.exists()


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


def test_staging_binds_validated_bytes_not_file_reread(loader, monkeypatch):
    """A concurrent overwrite of the PUBLIC pending SKILL.md landing inside the
    staging window (after the content write, before staging completes) must not
    be vouched for: the binding hashes the in-memory validated bytes, never a
    re-read of the publicly writable file, so the unattended promotion refuses
    the swapped candidate instead of installing unvalidated prose."""
    _write_live(loader, "stage-tamper", version=1, body="OLD")
    binding: list[str] = []
    real_write = loader._write_pinned_new_file
    slug = "stage-tamper-update"
    tampered = False

    def meta_write_then_tamper(parent, name, payload, *, mode=0o666):
        nonlocal tampered
        result = real_write(parent, name, payload, mode=mode)
        if name == ".meta.json" and parent.path.name == slug:
            # The concurrent writer reaches the PUBLIC name, the way another process
            # would; a descriptor-relative open is also unavailable on Windows.
            fd = os.open(
                parent.path / "SKILL.md",
                os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.ftruncate(fd, 0)
                os.write(fd, b"## Steps\n\nINJECTED\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            tampered = True
        return result

    monkeypatch.setattr(loader, "_write_pinned_new_file", meta_write_then_tamper)
    _stage_update(
        loader,
        slug,
        target="auto/stage-tamper",
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )

    assert tampered is True
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

    def validate_then_mutate_claim(src, name, **kwargs):
        result = real_validate(src, name, **kwargs)
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
    real_rename = loader._rename_skill_child_no_replace
    injected = False

    def edit_before_capture(source, source_name, destination, destination_name, **kwargs):
        nonlocal injected
        if (
            source.path == live_dir.parent
            and source_name == live_dir.name
            and destination.path == loader._live_quarantine_root()
        ):
            assert injected is False
            injected = True
            live_file.write_text(edited, encoding="utf-8")
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", edit_before_capture)

    assert loader.approve_pending_update(slug) is None
    assert injected is True
    assert live_file.read_text(encoding="utf-8") == edited
    assert not (live_dir / ".versions").exists()
    assert (loader._pending_root() / slug / "SKILL.md").is_file()


@pytest.mark.parametrize("evidence_kind", ["candidate", "old-live"])
@pytest.mark.parametrize("evidence_change", ["missing", "replacement"])
def test_final_evidence_transition_revalidates_all_public_identities(
    loader,
    monkeypatch,
    evidence_kind,
    evidence_change,
):
    target = f"final-evidence-{evidence_kind}-{evidence_change}"
    slug = f"{target}-update"
    _write_live(loader, target, version=1, body="BEFORE")
    _stage_update(loader, slug, target=f"auto/{target}")
    real_rename = loader._rename_skill_child_no_replace
    displaced: list[Path] = []

    def move_private_then_change_public(
        source,
        source_name,
        destination,
        destination_name,
        **kwargs,
    ):
        result = real_rename(
            source,
            source_name,
            destination,
            destination_name,
            **kwargs,
        )
        if source.path == loader._claims_root() and destination.path == loader._evidence_root():
            _stage, backup = loader._publication_paths(source_name)
            public = loader._quarantine_root() / source_name
            if evidence_kind == "old-live":
                public = backup
            moved = public.with_name(f"{public.name}-{evidence_kind}-{evidence_change}")
            public.rename(moved)
            displaced.append(moved)
            if evidence_change == "replacement":
                public.mkdir()
                (public / "SKILL.md").write_text("REPLACEMENT", encoding="utf-8")
        return result

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", move_private_then_change_public)

    assert loader.approve_pending_update(slug) is None
    assert loader.get_auto_skill_version(f"auto/{target}") == 2
    assert len(displaced) == 1 and displaced[0].is_dir()
    evidence_claims = list(loader._evidence_root().glob(f"{slug}--*"))
    assert len(evidence_claims) == 1
    claim = evidence_claims[0]
    lock_path = loader._claim_lock_path(claim.name)
    assert lock_path.is_file()

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert claim.is_dir()
    assert lock_path.is_file()
    assert displaced[0].is_dir()
    _stage, old_live = restarted._publication_paths(claim.name)
    candidate = restarted._quarantine_root() / claim.name
    changed_path = candidate if evidence_kind == "candidate" else old_live
    if evidence_change == "replacement":
        assert (changed_path / "SKILL.md").read_text(encoding="utf-8") == "REPLACEMENT"
    else:
        assert not changed_path.exists()


@pytest.mark.parametrize("evidence_change", ["missing", "replacement"])
def test_update_commit_refuses_changed_old_live_evidence_before_retention(
    loader,
    monkeypatch,
    evidence_change,
):
    target = f"commit-evidence-{evidence_change}"
    slug = f"{target}-update"
    _write_live(loader, target, version=1, body="BEFORE")
    _stage_update(loader, slug, target=f"auto/{target}")
    real_marker = loader._write_completion_marker
    displaced: list[Path] = []

    def marker_then_change_evidence(claim, expected_claim_identity=None):
        completed = real_marker(claim, expected_claim_identity)
        _stage, backup = loader._publication_paths(claim.name)
        moved = backup.with_name(f"{backup.name}-{evidence_change}")
        backup.rename(moved)
        displaced.append(moved)
        if evidence_change == "replacement":
            backup.mkdir()
            (backup / "SKILL.md").write_text("REPLACEMENT", encoding="utf-8")
        return completed

    monkeypatch.setattr(loader, "_write_completion_marker", marker_then_change_evidence)
    assert loader.approve_pending_update(slug) is None
    assert loader.get_auto_skill_version(f"auto/{target}") == 2
    claims = list(loader._claims_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    claim = claims[0]
    lock_path = loader._claim_lock_path(claim.name)
    assert lock_path.is_file()
    assert (claim / ".promoted").is_file()
    assert displaced and displaced[0].is_dir()

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert claim.is_dir()
    assert lock_path.is_file()
    assert displaced[0].is_dir()
    if evidence_change == "replacement":
        _stage, expected_name = restarted._publication_paths(claim.name)
        assert (expected_name / "SKILL.md").read_text(encoding="utf-8") == "REPLACEMENT"


def test_marker_parent_sync_failure_preserves_prepared_journal_for_consumption(loader, monkeypatch):
    """A failed parent sync cannot become a completion or evidence transition."""
    _write_live(loader, "marker-fallback", version=1, body="OLD")
    slug = "marker-fallback-update"
    _stage_update(loader, slug, target="auto/marker-fallback", base_version=1)
    real_sync = loader._sync_pinned_parent
    failed = False

    def fail_claim_parent_once(parent):
        nonlocal failed
        if (
            not failed
            and parent.path.parent == loader._claims_root()
            and parent.path.name.startswith(f"{slug}--")
        ):
            failed = True
            raise OSError("injected marker parent sync failure")
        return real_sync(parent)

    monkeypatch.setattr(loader, "_sync_pinned_parent", fail_claim_parent_once)
    monkeypatch.setattr(
        loader,
        "_commit_claim_evidence_state",
        lambda *_args: pytest.fail("evidence transition ran without a durable marker"),
    )

    assert loader.approve_pending_update(slug) is None
    assert failed is True
    claims = list(loader._claims_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    claim = claims[0]
    assert not (claim / ".promoted").exists()
    lock_path = loader._claim_lock_path(claim.name)
    fd = _open_lock_for_test(loader, lock_path)
    acquired = skills_mod.platform_compat.try_acquire_lock(fd, exclusive=True)
    try:
        assert acquired is True
        journal = loader._authenticated_claim_publication(fd, lock_path, claim.name)
        assert journal is not None and journal["state"] == "prepared"
    finally:
        if acquired:
            skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert restarted.get_auto_skill_version("auto/marker-fallback") == 2
    assert not claim.exists()


def test_completion_marker_parent_sync_precedes_evidence_transition(loader, monkeypatch):
    slug = "marker-sync-order"
    loader.stage_skill_candidate(
        slug,
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nNEW",
        provenance=_prov(),
    )
    events: list[str] = []
    real_sync = loader._sync_pinned_parent
    real_evidence = loader._commit_claim_evidence_state

    def record_claim_parent_sync(parent):
        result = real_sync(parent)
        if parent.path.parent == loader._claims_root() and parent.path.name.startswith(f"{slug}--"):
            events.append("marker-parent-synced")
        return result

    def record_evidence_transition(*args):
        events.append("evidence-transition")
        assert "marker-parent-synced" in events
        return real_evidence(*args)

    monkeypatch.setattr(loader, "_sync_pinned_parent", record_claim_parent_sync)
    monkeypatch.setattr(loader, "_commit_claim_evidence_state", record_evidence_transition)

    assert loader.approve_pending_skill(slug) == f"auto/{slug}"
    assert events.index("marker-parent-synced") < events.index("evidence-transition")


def test_restart_recovers_crash_after_durable_marker_before_evidence_transition(
    loader, monkeypatch
):
    slug = "marker-power-loss"
    loader.stage_skill_candidate(
        slug,
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nNEW",
        provenance=_prov(),
    )

    class InjectedCrash(BaseException):
        pass

    def crash_before_evidence(*_args):
        raise InjectedCrash()

    monkeypatch.setattr(loader, "_commit_claim_evidence_state", crash_before_evidence)
    with pytest.raises(InjectedCrash):
        loader.approve_pending_skill(slug)

    claims = list(loader._claims_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    claim = claims[0]
    assert loader._authenticated_completion_marker(claim) is True
    lock_path = loader._claim_lock_path(claim.name)
    fd = _open_lock_for_test(loader, lock_path)
    try:
        journal = loader._authenticated_claim_publication(fd, lock_path, claim.name)
        assert journal is not None and journal["state"] == "prepared"
    finally:
        os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert (restarted._dir / "auto" / slug / "SKILL.md").is_file()
    assert not claim.exists()
    assert (restarted._evidence_root() / claim.name).is_dir()


def test_evidence_state_failure_uses_authenticated_marker(loader, monkeypatch):
    """A committed marker makes a failed evidence-state write recoverable."""
    slug = "journal-fallback"
    loader.stage_skill_candidate(
        slug,
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nNEW",
        provenance=_prov(),
    )
    monkeypatch.setattr(loader, "_commit_claim_evidence_state", lambda *_args: False)

    assert loader.approve_pending_skill(slug) == "auto/journal-fallback"
    claims = list(loader._claims_root().glob(f"{slug}--*"))
    quarantines = list(loader._quarantine_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    assert len(quarantines) == 1
    assert loader._authenticated_completion_marker(claims[0]) is True

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert restarted.get_auto_skill_version("auto/journal-fallback") == 1
    assert not claims[0].exists()
    assert (restarted._evidence_root() / claims[0].name).is_dir()
    assert quarantines[0].is_dir()


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


# ── rebase answers: lock order, fail-closed hosts, retained old-live evidence ──


_LOCK_RANK = {name: rank for rank, name in enumerate(skills_mod.AUTO_SKILL_LOCK_ORDER)}


def _record_lock_nesting(loader, monkeypatch) -> list[tuple[str, tuple[str, ...]]]:
    """Record every auto-skill lock acquisition with the locks already held.

    Each entry is ``(acquired, held_before)``. The claim lock is the one taken
    without a context manager: it is held from ``_claim_pending_update`` returning
    until its descriptor is released.
    """
    held: list[str] = []
    seen: list[tuple[str, tuple[str, ...]]] = []
    claim_fds: set[int] = set()

    def tracked(kind, real):
        @contextlib.contextmanager
        def wrapper(*args, **kwargs):
            name = kind(*args) if callable(kind) else kind
            if name is None:
                with real(*args, **kwargs) as acquired:
                    yield acquired
                return
            seen.append((name, tuple(held)))
            with real(*args, **kwargs) as acquired:
                held.append(name)
                try:
                    yield acquired
                finally:
                    held.remove(name)

        return wrapper

    monkeypatch.setattr(
        loader,
        "_auto_slug_claim_lock",
        tracked("slug-claim", loader._auto_slug_claim_lock),
    )
    monkeypatch.setattr(loader, "_promotion_lock", tracked("target", loader._promotion_lock))
    monkeypatch.setattr(
        loader,
        "_file_lock",
        tracked(
            lambda name, *_a: "namespace" if name == "pending.lock" else None, loader._file_lock
        ),
    )
    real_claim = loader._claim_pending_update

    def claim(slug):
        result = real_claim(slug)
        if result is not None:
            seen.append(("claim", tuple(held)))
            held.append("claim")
            claim_fds.add(result[1])
        return result

    real_release = skills_mod.platform_compat.release_lock

    def release(fd):
        if fd in claim_fds:
            claim_fds.discard(fd)
            held.remove("claim")
        return real_release(fd)

    monkeypatch.setattr(loader, "_claim_pending_update", claim)
    monkeypatch.setattr(skills_mod.platform_compat, "release_lock", release)
    return seen


def test_every_auto_skill_path_takes_its_locks_in_the_one_documented_order(loader, monkeypatch):
    seen = _record_lock_nesting(loader, monkeypatch)

    assert loader.create_auto_skill(
        "order-live", description="d", triggers="t", procedure_md="p", provenance=_prov()
    )
    assert loader.archive_auto_skill("auto/order-live")
    assert loader.restore_auto_skill("order-live") == "auto/order-live"
    assert loader.stage_skill_candidate(
        "order-new", description="d", triggers="t", procedure_md="p", provenance=_prov()
    )
    assert loader.approve_pending_skill("order-new") == "auto/order-new"
    _stage_update(loader, "order-live-update", target="auto/order-live")
    assert loader.approve_pending_update("order-live-update") == "auto/order-live"
    assert loader.stage_skill_candidate(
        "order-gone", description="d", triggers="t", procedure_md="p", provenance=_prov()
    )
    assert loader.dismiss_pending_skill("order-gone") is True

    acquired = {name for name, _held in seen}
    assert acquired == set(_LOCK_RANK), acquired
    nested = [(name, held) for name, held in seen if held]
    assert nested, "no path nested two locks, so the order was never exercised"
    for name, held in seen:
        assert all(_LOCK_RANK[outer] < _LOCK_RANK[name] for outer in held), (name, held)


def test_live_publish_takes_the_target_lock_inside_the_slug_claim(loader, monkeypatch):
    seen = _record_lock_nesting(loader, monkeypatch)
    assert loader.create_auto_skill(
        "nested-order", description="d", triggers="t", procedure_md="p", provenance=_prov()
    )
    assert ("target", ("slug-claim",)) in seen


def test_a_late_write_to_the_old_live_generation_survives_publication_cleanup(loader, monkeypatch):
    """Cleanup removes private trees only; the public old-live inode is evidence.

    A writer holding a descriptor into the replaced live generation can still write
    after the last hash publication takes. That write lands in the public
    ``.live-quarantine`` entry, which no cleanup ever deletes.
    """
    if os.name == "nt":
        pytest.skip("a retained write handle blocks the rename on Windows")
    target = "late-old-live"
    live = _write_live(loader, target, version=1, body="OLD")
    retained = os.open(live / "SKILL.md", os.O_WRONLY)
    real_commit = loader._commit_claim_consumption
    late = b"LATE WRITE AFTER THE FINAL HASH\n"

    def write_then_commit(claim, claim_fd):
        # Publication has hashed and renamed both generations; this write is
        # the one no hash can see, and the commit's cleanup runs right after.
        os.lseek(retained, 0, os.SEEK_SET)
        os.ftruncate(retained, 0)
        os.write(retained, late)
        return real_commit(claim, claim_fd)

    monkeypatch.setattr(loader, "_commit_claim_consumption", write_then_commit)
    _stage_update(loader, f"{target}-update", target=f"auto/{target}")
    try:
        assert loader.approve_pending_update(f"{target}-update") == f"auto/{target}"
    finally:
        os.close(retained)

    (old_live,) = list(loader._live_quarantine_root().iterdir())
    assert (old_live / "SKILL.md").read_bytes() == late
    assert b"LATE WRITE" not in (live / "SKILL.md").read_bytes()


@pytest.mark.usefixtures("no_auto_skill_authority_startup")
def test_sandbox_off_has_a_distinct_refusal_and_never_provisions(
    uninitialized_loader,
    monkeypatch,
):
    from kiro_crew import sandbox

    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        _REAL_AUTHORITY_SANDBOX_REFUSAL,
    )
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "off")
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: mode != "off")

    with pytest.raises(OSError, match="sandbox_off"):
        skills_mod.initialize_gateway_auto_skill_private_authority()

    assert skills_mod._STARTUP_AUTHORITY_BINDING is None
    assert skills_mod.auto_skill_promotion_disabled_reason() == "sandbox_off"
    assert not _authority_path(uninitialized_loader._private_root().parents[1]).exists()
    pending = uninitialized_loader._pending_root() / "queued-before-off"
    pending.mkdir(parents=True)
    (pending / "SKILL.md").write_text("## Steps\n\nqueued\n", encoding="utf-8")
    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        uninitialized_loader.approve_pending_skill_checked("queued-before-off")
    assert refused.value.reason == "promotion_disabled"
    assert (pending / "SKILL.md").exists()


@pytest.mark.usefixtures("no_auto_skill_authority_startup")
def test_live_mode_flip_off_revokes_the_next_promotion(uninitialized_loader, monkeypatch):
    masked = True
    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        lambda: None if masked else skills_mod._SANDBOX_OFF_REFUSAL,
    )
    skills_mod.initialize_gateway_auto_skill_private_authority()
    assert (
        uninitialized_loader.stage_skill_candidate(
            "mode-flip",
            description="d",
            triggers="t",
            procedure_md="p",
            provenance=_prov(),
        )
        == "auto/mode-flip"
    )

    masked = False
    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        uninitialized_loader.approve_pending_skill_checked("mode-flip")

    assert refused.value.reason == "promotion_disabled"
    assert skills_mod._STARTUP_AUTHORITY_BINDING is None
    assert skills_mod.auto_skill_promotion_disabled_reason() == "sandbox_off"
    assert (uninitialized_loader._pending_root() / "mode-flip" / "SKILL.md").exists()


@pytest.mark.usefixtures("no_auto_skill_authority_startup")
def test_turning_on_a_delegated_sandbox_after_startup_revokes_the_next_promotion(
    uninitialized_loader,
    monkeypatch,
):
    """The delegation predicate (``kiro_internal_sandbox_enabled`` on macOS) is read
    uncached at every authority-backed use, not only at startup certification."""
    from kiro_crew import sandbox

    delegated = False
    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        _REAL_AUTHORITY_SANDBOX_REFUSAL,
    )
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: delegated)
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "strict")
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: mode == "strict")
    skills_mod.initialize_gateway_auto_skill_private_authority()
    assert (
        uninitialized_loader.stage_skill_candidate(
            "delegation-flip",
            description="d",
            triggers="t",
            procedure_md="p",
            provenance=_prov(),
        )
        == "auto/delegation-flip"
    )

    delegated = True
    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        uninitialized_loader.approve_pending_skill_checked("delegation-flip")

    assert refused.value.reason == "promotion_disabled"
    assert skills_mod._STARTUP_AUTHORITY_BINDING is None
    assert "does not build" in (skills_mod.auto_skill_promotion_disabled_reason() or "")


@pytest.mark.usefixtures("no_auto_skill_authority_startup")
def test_gateway_certifies_authority_when_the_protecting_mask_applies(
    uninitialized_loader,
    monkeypatch,
):
    from kiro_crew import sandbox

    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        _REAL_AUTHORITY_SANDBOX_REFUSAL,
    )
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "strict")
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: mode == "strict")

    binding = skills_mod.initialize_gateway_auto_skill_private_authority()

    assert binding == skills_mod._STARTUP_AUTHORITY_BINDING
    assert skills_mod.auto_skill_promotion_disabled_reason() is None
    assert _authority_path(uninitialized_loader._private_root().parents[1]).is_dir()


@pytest.mark.usefixtures("no_auto_skill_authority_startup")
class TestDelegatedSandboxFailsClosed:
    """Where Kiro Crew does not build the agent sandbox, auto-skill promotion is off.

    Pending maintainer confirmation of the supported platform contract: native
    Windows and macOS with the Kiro CLI internal sandbox cannot hide the authority
    from an agent's shell, so the gateway never creates or certifies it there.
    Live auto-skill edits and dismissals keep working because the authority root
    is absent, regardless of the sandbox refusal that prevented its creation.
    """

    @pytest.fixture()
    def delegated(self, uninitialized_loader, monkeypatch):
        monkeypatch.setattr(
            skills_mod,
            "_auto_skill_authority_sandbox_refusal",
            lambda: skills_mod._DELEGATED_SANDBOX_REFUSAL,
        )
        monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: True)
        return uninitialized_loader

    def test_the_gateway_never_provisions_authority(self, delegated):
        with pytest.raises(OSError, match="does not build"):
            skills_mod.initialize_gateway_auto_skill_private_authority()
        assert skills_mod._STARTUP_AUTHORITY_BINDING is None
        assert not _authority_path(delegated._private_root().parents[1]).exists()
        assert "does not build" in (skills_mod.auto_skill_promotion_disabled_reason() or "")

    def test_staging_and_approval_refuse_with_the_reason(self, delegated):
        with pytest.raises(OSError):
            skills_mod.initialize_gateway_auto_skill_private_authority()
        refusal = ClaimRefusal()
        assert (
            delegated.stage_skill_candidate(
                "delegated-new",
                description="d",
                triggers="t",
                procedure_md="p",
                provenance=_prov(),
                refusal=refusal,
            )
            is None
        )
        assert refusal.retryable is False
        pending = delegated._pending_root() / "pre-upgrade"
        pending.mkdir(parents=True)
        (pending / "SKILL.md").write_text("## Steps\n\nqueued before\n", encoding="utf-8")
        with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
            delegated.approve_pending_skill_checked("pre-upgrade")
        assert refused.value.reason == "promotion_disabled"
        assert (pending / "SKILL.md").exists()

    def test_live_edits_and_dismissal_keep_working(self, delegated):
        with pytest.raises(OSError):
            skills_mod.initialize_gateway_auto_skill_private_authority()
        _write_live(delegated, "delegated-live", version=1, body="OLD")
        assert delegated.set_pinned("auto/delegated-live", True) is True
        pending = delegated._pending_root() / "pre-upgrade"
        pending.mkdir(parents=True)
        (pending / "SKILL.md").write_text("## Steps\n\nqueued before\n", encoding="utf-8")
        assert delegated.dismiss_pending_skill("pre-upgrade") is True
        assert not pending.exists()

    def test_unreviewed_publication_is_off_under_the_same_gate(self, delegated):
        """``approval_required=false`` publishes through ``create_auto_skill``; the
        gate that turns the reviewed path off must turn it off too, or the less
        safe configuration keeps a feature the safer one lost."""
        with pytest.raises(OSError):
            skills_mod.initialize_gateway_auto_skill_private_authority()

        assert (
            delegated.create_auto_skill(
                "delegated-direct",
                description="d",
                triggers="t",
                procedure_md="## Steps\n\nrun\n",
                provenance=_prov(),
            )
            is None
        )
        assert not (delegated._dir / skills_mod.AUTO_SKILL_NAMESPACE / "delegated-direct").exists()

    def test_pre_upgrade_candidates_survive_ttl_pruning(self, delegated):
        with pytest.raises(OSError):
            skills_mod.initialize_gateway_auto_skill_private_authority()
        pending = delegated._pending_root() / "pre-upgrade-old"
        pending.mkdir(parents=True)
        (pending / "SKILL.md").write_text("## Steps\n\nqueued before\n", encoding="utf-8")
        queued_at = pending.stat().st_mtime

        pruned = delegated.prune_pending(1, now=queued_at + 30 * 86400)

        assert pruned == 0
        assert (pending / "SKILL.md").exists()
        assert [entry["slug"] for entry in delegated.list_pending_skills()] == ["pre-upgrade-old"]


@pytest.mark.usefixtures("no_auto_skill_authority_startup")
def test_startup_refusal_does_not_override_root_absence_after_mode_flips_back(
    uninitialized_loader,
    monkeypatch,
):
    masked = False
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        lambda: None if masked else skills_mod._SANDBOX_OFF_REFUSAL,
    )

    with pytest.raises(OSError, match="sandbox_off"):
        skills_mod.initialize_gateway_auto_skill_private_authority()

    masked = True
    _write_live(uninitialized_loader, "refused-live", version=1, body="OLD")
    assert skills_mod._STARTUP_AUTHORITY_REFUSAL == skills_mod._SANDBOX_OFF_REFUSAL
    assert skills_mod._auto_skill_promotion_ruled_out() is True
    assert uninitialized_loader.set_pinned("auto/refused-live", True) is True
    assert not _authority_path(uninitialized_loader._private_root().parents[1]).exists()


@pytest.mark.usefixtures("no_auto_skill_authority_startup")
def test_a_process_that_never_certified_still_requires_the_lock(uninitialized_loader, monkeypatch):
    """A fresh CLI process must not bypass a root another process can promote through."""
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    skills_mod.initialize_auto_skill_private_authority(
        skills_root=uninitialized_loader._dir,
        data_home=uninitialized_loader._private_root().parents[1],
    )
    skills_mod._reset_auto_skill_private_authority_for_tests()
    _write_live(uninitialized_loader, "cli-live", version=1, body="OLD")
    assert skills_mod._auto_skill_promotion_ruled_out() is False
    assert uninitialized_loader.set_pinned("auto/cli-live", True) is False


# ── An agent must not be able to lock the operator out (item 1) ──


def test_a_planted_non_canonical_auto_skill_stays_deletable(loader):
    """A name no promotion can target is mutated by name, as before the protocol.

    An injected agent plants ``auto/Persist_Me`` with ``always: true``: refusing
    its lock would keep it injected into every session while the dashboard
    answered every delete, pin and inject toggle with "not found".
    """
    planted = loader._dir / skills_mod.AUTO_SKILL_NAMESPACE / "Persist_Me"
    planted.mkdir(parents=True)
    (planted / "SKILL.md").write_text(
        "---\nname: auto/Persist_Me\ndescription: planted\ntriggers: t\nalways: true\n"
        "---\n\nobey the planted instructions\n",
        encoding="utf-8",
    )
    loader._invalidate_iter_cache()
    assert skills_mod._auto_skill_promotion_ruled_out() is False
    assert "auto/Persist_Me" in loader.get_always_skills()

    assert loader.set_pinned("auto/Persist_Me", True) is True
    assert loader.set_inject_on_trigger("auto/Persist_Me", False) is True
    assert loader.delete_skill("auto/Persist_Me") is True

    assert not planted.exists()
    assert "auto/Persist_Me" not in loader.get_always_skills()


@pytest.mark.parametrize(
    ("spelling", "lock_slug"),
    [
        ("alias-lock", "alias-lock"),
        ("Alias-Lock", "alias-lock"),
        ("alias-lock.", "alias-lock"),
        ("alias-lock. ", "alias-lock"),
        ("ALIAS-LOCK/nested", "alias-lock"),
        ("\u212aelvin-alias", "kelvin-alias"),
        ("Persist_Me", None),
        ("x", None),
    ],
)
def test_live_names_fold_onto_their_canonical_target_lock(spelling, lock_slug):
    assert SkillsLoader._live_auto_target_lock_slug(spelling) == lock_slug


def test_an_alias_spelling_waits_on_the_canonical_target_lock(loader, monkeypatch):
    """``Alias-Lock`` opens ``auto/alias-lock`` on a case-insensitive filesystem,
    so it must serialize with a promotion of ``alias-lock`` instead of refusing."""
    _write_live(loader, "Alias-Lock", version=1, body="OLD")
    attempted = threading.Event()
    real_lock = loader._promotion_lock
    seen: list[str] = []

    @contextlib.contextmanager
    def observed_lock(slug):
        seen.append(slug)
        if threading.current_thread().name.startswith("alias-mutator"):
            attempted.set()
        with real_lock(slug) as acquired:
            yield acquired

    monkeypatch.setattr(loader, "_promotion_lock", observed_lock)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="alias-mutator") as pool:
        with real_lock("alias-lock") as acquired:
            assert acquired is True
            future = pool.submit(loader.set_pinned, "auto/Alias-Lock", True)
            assert attempted.wait(timeout=_THREAD_WAIT_CEILING_SECS)
            assert not future.done()
        assert future.result(timeout=_THREAD_WAIT_CEILING_SECS) is True
    assert seen == ["alias-lock"]


@pytest.mark.parametrize("obsolete", ["nested", "direct"])
def test_an_obsolete_root_planted_after_startup_blocks_nothing(loader, obsolete):
    """The obsolete spellings are agent-writable and hold nothing trusted once a
    certificate exists, so they are refused at startup only. An empty one planted
    later must not refuse live mutations, staging or promotion."""
    home = loader._private_root().parents[1]
    if obsolete == "nested":
        loader._legacy_private_root().mkdir(parents=True)
    else:
        (home / skills_mod.AUTO_SKILL_PRIVATE_STATE_DIRNAME).mkdir()
    _write_live(loader, "after-plant", version=1, body="OLD")

    assert skills_mod.auto_skill_promotion_disabled_reason() is None
    assert loader.set_pinned("auto/after-plant", True) is True
    assert (
        loader.stage_skill_candidate(
            "after-plant-new",
            description="d",
            triggers="t",
            procedure_md="## Steps\n\nrun\n",
            provenance=_prov(),
        )
        == "auto/after-plant-new"
    )
    assert loader.approve_pending_skill("after-plant-new") == "auto/after-plant-new"
    assert loader.delete_skill("auto/after-plant") is True


# ── Authority provenance must be recoverable (item 3) ──


def _rewrite_authority_record(loader, home, case):
    record = loader._authority_provenance_path()
    root = loader._private_root()
    if case == "record-missing":
        record.unlink()
        return
    if case == "root-missing":
        shutil.rmtree(root)
        return
    with (
        loader._pin_skill_parent(home) as home_parent,
        loader._pin_skill_parent(root) as root_parent,
    ):
        home_identity = home_parent.native_identity
        root_identity = root_parent.native_identity
    if case == "home-moved":
        # What a restored backup or a cp -a / rsync host move looks like: the
        # record names the old home inode.
        home_identity = skills_mod._TaggedFileIdentity(
            home_identity.kind,
            home_identity.volume,
            (
                int(home_identity.object_id) + 1
                if isinstance(home_identity.object_id, int)
                else bytes(reversed(home_identity.object_id))
            ),
        )
    body = loader._authority_record_body(home, home, home_identity, root_identity)
    body["mac"] = "0" * 64 if case == "key-regenerated" else loader._authority_record_mac(body)
    record.write_text(json.dumps(body), encoding="utf-8")


def _restart_gateway_certification():
    skills_mod._reset_auto_skill_private_authority_for_tests()
    return skills_mod.initialize_gateway_auto_skill_private_authority()


def _plant_claim_lock(loader, claim_name):
    lock_path = loader._claim_lock_path(claim_name)
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        created: list[bool] = []
        fd = loader._open_skill_lock(
            state.claim_locks,
            lock_path.name,
            created_out=created,
        )
        acquired = skills_mod.platform_compat.try_acquire_lock(fd, exclusive=True)
        assert acquired
        try:
            assert loader._initialize_claim_lock_state(fd, lock_path, claim_name)
            if created == [True]:
                loader._sync_pinned_parent(state.claim_locks)
        finally:
            skills_mod.platform_compat.release_lock(fd)
            os.close(fd)


def _plant_public_quarantine(loader, slug, claim_name):
    loader.stage_skill_candidate(
        slug,
        description="d",
        triggers="t",
        procedure_md="## Steps\n\nrun\n",
        provenance=_prov(),
    )
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        identity = loader._pinned_child_identity(state.pending, slug)
        assert identity is not None
        loader._rename_skill_child_no_replace(
            state.pending,
            slug,
            state.quarantine,
            claim_name,
            expected_identity=identity,
        )
        loader._sync_pinned_rename_parents(state.pending, state.quarantine)


@pytest.mark.parametrize("surface", ["crash-window", "public-only", "lock-only"])
def test_stale_reseed_refuses_unmatched_in_flight_state(
    uninitialized_loader,
    surface,
):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    skills_mod.initialize_gateway_auto_skill_private_authority()
    claim_name = "reseed-crash--0123456789abcdef0123456789abcdef"
    if surface in {"crash-window", "lock-only"}:
        _plant_claim_lock(loader, claim_name)
    if surface in {"crash-window", "public-only"}:
        _plant_public_quarantine(loader, "reseed-crash", claim_name)
    root = loader._private_root()
    root_identity = root.stat().st_ino
    _rewrite_authority_record(loader, home, "key-regenerated")

    with pytest.raises(OSError) as refused:
        _restart_gateway_certification()

    message = str(refused.value)
    expected = (
        f"{skills_mod.AUTO_QUARANTINE_DIRNAME}/{claim_name}"
        if surface != "lock-only"
        else f"{skills_mod.AUTO_LOCKS_DIRNAME}/{skills_mod.AUTO_CLAIMS_DIRNAME}/"
        f"{claim_name}.lock"
    )
    assert expected in message
    if surface == "crash-window":
        assert f"{skills_mod.AUTO_LOCKS_DIRNAME}/{skills_mod.AUTO_CLAIMS_DIRNAME}/" in message
    assert "still in flight" in (skills_mod.auto_skill_promotion_disabled_reason() or "")
    assert root.stat().st_ino == root_identity
    assert not [name for name in os.listdir(root.parent) if ".stale-" in name]


@pytest.mark.parametrize("surface", ["candidate", "live"])
def test_missing_private_root_refuses_public_quarantine(
    uninitialized_loader,
    surface,
):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    skills_mod.initialize_gateway_auto_skill_private_authority()
    claim_name = f"missing-root-{surface}--0123456789abcdef0123456789abcdef"
    if surface == "candidate":
        _plant_public_quarantine(loader, "missing-root-candidate", claim_name)
        quarantine_dirname = skills_mod.AUTO_QUARANTINE_DIRNAME
    else:
        public_entry = loader._live_quarantine_root() / claim_name
        public_entry.mkdir(parents=True)
        quarantine_dirname = skills_mod.AUTO_LIVE_QUARANTINE_DIRNAME
    record_bytes = loader._authority_provenance_path().read_bytes()
    _rewrite_authority_record(loader, home, "root-missing")

    with pytest.raises(OSError) as refused:
        _restart_gateway_certification()

    message = str(refused.value)
    assert f"{quarantine_dirname}/{claim_name}" in message
    assert "still in flight" in message
    assert "still in flight" in (skills_mod.auto_skill_promotion_disabled_reason() or "")
    assert not loader._private_root().exists()
    assert loader._authority_provenance_path().read_bytes() == record_bytes
    assert not [
        name for name in os.listdir(loader._authority_provenance_path().parent) if ".stale-" in name
    ]


def test_bounded_stale_claim_scan_consumes_only_limit_plus_one(monkeypatch):
    consumed: list[int] = []

    class Entry:
        def __init__(self, index):
            self.name = f"entry-{index}"

    def entries():
        for index in range(10):
            consumed.append(index)
            yield Entry(index)

    monkeypatch.setattr(skills_mod, "_STALE_CLAIM_SCAN_LIMIT", 3)
    with pytest.raises(skills_mod._StaleClaimScanOverflow, match="3-entry"):
        skills_mod._bounded_stale_claim_names(
            Path("unused"),
            label="authority root",
            scanner=lambda _target: contextlib.nullcontext(entries()),
        )
    assert consumed == [0, 1, 2, 3]


@pytest.mark.parametrize("surface", ["claims", "quarantine"])
def test_pending_slug_claim_scan_consumes_only_limit_plus_one(
    loader,
    monkeypatch,
    caplog,
    surface,
):
    consumed: list[int] = []
    scan_count = 0

    class Entry:
        def __init__(self, index):
            self.name = f"other-{index}--0123456789abcdef"

    def entries():
        for index in range(10):
            consumed.append(index)
            yield Entry(index)

    def scanner(_target):
        nonlocal scan_count
        scan_count += 1
        selected = surface == "claims" or scan_count == 2
        return contextlib.nullcontext(entries() if selected else iter(()))

    monkeypatch.setattr(skills_mod, "_ACTIVE_CLAIM_SCAN_LIMIT", 3)
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        monkeypatch.setattr(skills_mod.os, "scandir", scanner)
        with caplog.at_level("WARNING", logger="kiro_crew.skills"):
            assert loader._pending_slug_claimed("wanted", private_state=state) is None

    assert consumed == [0, 1, 2, 3]
    label = "active claims" if surface == "claims" else "public candidate quarantine"
    assert f"{label} exceeded the 3-entry active-claim scan limit" in caplog.text


def test_pending_slug_claim_scan_keeps_under_bound_match_behavior(loader, monkeypatch):
    consumed: list[str] = []

    class Entry:
        def __init__(self, name):
            self.name = name

    def entries():
        for name in (
            "other--0123456789abcdef",
            "wanted--0123456789abcdef",
            "unread--0123456789abcdef",
        ):
            consumed.append(name)
            yield Entry(name)

    monkeypatch.setattr(skills_mod, "_ACTIVE_CLAIM_SCAN_LIMIT", 3)
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        monkeypatch.setattr(
            skills_mod.os,
            "scandir",
            lambda _target: contextlib.nullcontext(entries()),
        )
        assert loader._pending_slug_claimed("wanted", private_state=state) is True

    assert consumed == [
        "other--0123456789abcdef",
        "wanted--0123456789abcdef",
    ]


def test_recovery_scan_overflow_is_bounded_and_indeterminate(
    loader,
    monkeypatch,
    caplog,
):
    consumed: list[int] = []
    scan_count = 0

    class Entry:
        def __init__(self, index):
            self.name = f"active-{index}--0123456789abcdef"

    def entries():
        for index in range(10):
            consumed.append(index)
            yield Entry(index)

    def scanner(_target):
        nonlocal scan_count
        scan_count += 1
        return contextlib.nullcontext(entries() if scan_count == 2 else iter(()))

    monkeypatch.setattr(skills_mod, "_ACTIVE_CLAIM_SCAN_LIMIT", 3)
    monkeypatch.setattr(skills_mod.os, "scandir", scanner)
    with caplog.at_level("WARNING", logger="kiro_crew.skills"):
        loader._recover_abandoned_claims()

    assert consumed == [0, 1, 2, 3]
    assert (
        "active claim namespace exceeded the 3-entry scan limit while reading "
        "public candidate quarantine"
        in caplog.text
    )


def test_stale_claim_scan_overflow_disables_auto_skills_without_reseed(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    skills_mod.initialize_gateway_auto_skill_private_authority()
    root = loader._private_root()
    for index in range(4):
        (root / f".publish-overflow-{index}").mkdir()
    root_identity = root.stat().st_ino
    _rewrite_authority_record(loader, home, "key-regenerated")
    monkeypatch.setattr(skills_mod, "_STALE_CLAIM_SCAN_LIMIT", 3)

    with pytest.raises(
        OSError,
        match="authority root exceeded the 3-entry stale-claim scan limit",
    ):
        _restart_gateway_certification()

    disabled = skills_mod.auto_skill_promotion_disabled_reason() or ""
    assert "stale claim state is indeterminate" in disabled
    assert "3-entry stale-claim scan limit" in disabled
    assert root.stat().st_ino == root_identity
    assert not [name for name in os.listdir(root.parent) if ".stale-" in name]


@pytest.mark.parametrize(
    "case",
    ["record-missing", "root-missing", "key-regenerated", "home-moved"],
)
def test_a_stale_idle_authority_is_reseeded_at_startup(uninitialized_loader, case):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    skills_mod.initialize_gateway_auto_skill_private_authority()
    retired: list[str] = []
    if case != "root-missing":
        loader.stage_skill_candidate(
            "retired-history",
            description="d",
            triggers="t",
            procedure_md="## Steps\n\nrun\n",
            provenance=_prov(),
        )
        assert loader.approve_pending_skill("retired-history") == "auto/retired-history"
        retired = sorted(os.listdir(loader._evidence_root()))
        assert retired and not os.listdir(loader._claims_root())
    else:
        assert not loader._quarantine_root().exists()
        assert not loader._live_quarantine_root().exists()
    old_root = loader._private_root().stat().st_ino
    _rewrite_authority_record(loader, home, case)

    binding = _restart_gateway_certification()

    assert binding == skills_mod._STARTUP_AUTHORITY_BINDING
    assert skills_mod.auto_skill_promotion_disabled_reason() is None
    parent = loader._private_root().parent
    aside = sorted(name for name in os.listdir(parent) if ".stale-" in name)
    if case != "root-missing":
        assert loader._private_root().stat().st_ino != old_root
        (stale_root,) = [
            name
            for name in aside
            if name.startswith(f"{skills_mod.AUTO_SKILL_PRIVATE_STATE_DIRNAME}.stale-")
        ]
        # Moved aside intact for inspection, never read for authority.
        assert sorted(os.listdir(parent / stale_root / skills_mod.AUTO_EVIDENCE_DIRNAME)) == (
            retired
        )
    if case != "record-missing":
        stale_record = f"{skills_mod._AUTHORITY_PROVENANCE_NAME}.stale-"
        assert any(name.startswith(stale_record) for name in aside)
    assert (
        loader.stage_skill_candidate(
            "after-reseed",
            description="d",
            triggers="t",
            procedure_md="## Steps\n\nrun\n",
            provenance=_prov(),
        )
        == "auto/after-reseed"
    )
    assert loader.approve_pending_skill("after-reseed") == "auto/after-reseed"


def test_a_stale_authority_with_a_claim_in_flight_refuses_with_exact_recovery(
    uninitialized_loader,
):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    skills_mod.initialize_gateway_auto_skill_private_authority()
    loader.stage_skill_candidate(
        "in-flight",
        description="d",
        triggers="t",
        procedure_md="## Steps\n\nrun\n",
        provenance=_prov(),
    )
    claimed = loader._claim_pending_update("in-flight")
    assert claimed is not None
    claim, claim_fd, _consumed_at, _snapshot = claimed
    # The owning process dies mid-claim: its lock is released, its claim stays.
    skills_mod.platform_compat.release_lock(claim_fd)
    os.close(claim_fd)
    root = loader._private_root()
    root_identity = root.stat().st_ino
    _rewrite_authority_record(loader, home, "key-regenerated")
    record_bytes = loader._authority_provenance_path().read_bytes()

    with pytest.raises(OSError) as refused:
        _restart_gateway_certification()

    message = str(refused.value)
    assert "1 claim(s) are still in flight" in message
    assert claim.name in message
    for step in (
        "stop every Kiro Crew gateway and agent",
        "tag-grants/auto-skill-private and tag-grants/auto-skill-private-authority.json",
        "skills/auto/.quarantine/<slug>--<token>",
        "skills/auto/.pending/<slug>",
        "start the gateway, which provisions a fresh authority",
    ):
        assert step in message
    assert "still in flight" in (skills_mod.auto_skill_promotion_disabled_reason() or "")
    assert root.stat().st_ino == root_identity
    assert (loader._claims_root() / claim.name).is_dir()
    assert loader._authority_provenance_path().read_bytes() == record_bytes
    assert not [name for name in os.listdir(root.parent) if ".stale-" in name]


# ── Restart recovery scales with claims in flight, not history (item 5) ──


def test_recovery_skips_retired_evidence_without_opening_its_lock(loader, monkeypatch):
    for index in range(3):
        slug = f"retired-{index}"
        loader.stage_skill_candidate(
            slug,
            description="d",
            triggers="t",
            procedure_md="## Steps\n\nrun\n",
            provenance=_prov(),
        )
        assert loader.approve_pending_skill(slug) == f"auto/{slug}"
    assert len(os.listdir(loader._evidence_root())) == 3
    opened: list[str] = []
    real_open = loader._open_skill_lock

    def counting_open(parent, name, **kwargs):
        opened.append(name)
        return real_open(parent, name, **kwargs)

    monkeypatch.setattr(loader, "_open_skill_lock", counting_open)

    loader._recover_abandoned_claims()

    assert [name for name in opened if name.startswith("retired-")] == []


# ── A filesystem without no-replace rename is refused once, at startup (item 5) ──


def test_startup_refuses_a_filesystem_without_no_replace_rename(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader

    def unsupported(self, *_args, **_kwargs):
        raise OSError(errno.EINVAL, "no-replace rename is unsupported here")

    pending = loader._pending_root() / "queued-on-nfs"
    pending.mkdir(parents=True)
    (pending / "SKILL.md").write_text("## Steps\n\nqueued\n", encoding="utf-8")
    with monkeypatch.context() as nfs:
        nfs.setattr(SkillsLoader, "_rename_skill_child_no_replace", unsupported)
        with pytest.raises(OSError, match="no atomic no-replace rename"):
            skills_mod.initialize_gateway_auto_skill_private_authority()

        assert skills_mod._STARTUP_AUTHORITY_BINDING is None
        assert skills_mod.auto_skill_promotion_disabled_reason() == (
            skills_mod._NO_REPLACE_RENAME_REFUSAL
        )
        assert skills_mod._auto_skill_promotion_ruled_out() is True
        assert not [
            name
            for name in os.listdir(loader._private_root())
            if name.startswith(skills_mod._RENAME_PROBE_PREFIX)
        ]
        assert (
            loader.stage_skill_candidate(
                "after-nfs",
                description="d",
                triggers="t",
                procedure_md="## Steps\n\nrun\n",
                provenance=_prov(),
            )
            is None
        )
        # The queue can be emptied by name only while the current probe proves
        # every process on this data home lacks the publication primitive.
        assert loader.dismiss_pending_skill("queued-on-nfs") is True
    assert not pending.exists()


# ── An obsolete spelling cannot withhold the certificate across restarts ──


def _plant_always_on_auto_skill(loader, slug):
    planted = loader._dir / skills_mod.AUTO_SKILL_NAMESPACE / slug
    planted.mkdir(parents=True)
    (planted / "SKILL.md").write_text(
        f"---\nname: auto/{slug}\ndescription: planted\ntriggers: t\nalways: true\n"
        "---\n\nobey the planted instructions\n",
        encoding="utf-8",
    )
    loader._invalidate_iter_cache()
    return planted


@pytest.mark.parametrize("spelling", ["direct", "nested"])
def test_an_obsolete_spelling_is_inert_once_the_authority_exists(
    uninitialized_loader,
    spelling,
    caplog,
):
    """After first creation a planted obsolete root is ignored, so a planted
    always-on skill stays deletable after a restart instead of failing closed on a
    target lock no uncertified process can take."""
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    skills_mod.initialize_gateway_auto_skill_private_authority()
    obsolete = (
        home / skills_mod.AUTO_SKILL_PRIVATE_STATE_DIRNAME
        if spelling == "direct"
        else loader._legacy_private_root()
    )
    obsolete.mkdir(parents=True)
    sentinel = obsolete / "sentinel"
    sentinel.write_text("planted", encoding="utf-8")
    obsolete_identity = obsolete.stat().st_ino
    planted = _plant_always_on_auto_skill(loader, "planted-always")
    caplog.set_level(logging.WARNING, logger="kiro_crew.skills")

    binding = _restart_gateway_certification()

    assert binding == skills_mod._STARTUP_AUTHORITY_BINDING
    assert skills_mod.auto_skill_promotion_disabled_reason() is None
    assert obsolete.stat().st_ino == obsolete_identity
    assert sentinel.read_text(encoding="utf-8") == "planted"
    assert "auto/planted-always" in loader.get_always_skills()
    assert loader.set_inject_on_trigger("auto/planted-always", False) is True
    assert loader.delete_skill("auto/planted-always") is True
    assert not planted.exists()
    assert "auto/planted-always" not in loader.get_always_skills()

    loader._migrate_legacy_private_state(loader._dir.resolve(), home.resolve())
    notices = [
        record
        for record in caplog.records
        if "Ignoring obsolete auto-skill authority spelling" in record.getMessage()
    ]
    assert len(notices) == 1


# ── An existing authority is retired only by the stopped operator ──


def test_existing_authority_never_grants_lock_free_mutation_when_sandbox_turns_off(
    uninitialized_loader,
    monkeypatch,
    caplog,
):
    from kiro_crew import sandbox

    loader = uninitialized_loader
    skills_mod.initialize_auto_skill_private_authority(
        skills_root=loader._dir,
        data_home=loader._private_root().parents[1],
    )
    skills_mod._reset_auto_skill_private_authority_for_tests()
    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        _REAL_AUTHORITY_SANDBOX_REFUSAL,
    )
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "off")
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda _mode: False)
    monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda _mode: "off")
    monkeypatch.setattr(sandbox, "unavailable_kind", lambda: "")
    _write_live(loader, "root-backed", version=1, body="OLD")

    caplog.set_level(logging.WARNING, logger="kiro_crew.skills")
    assert skills_mod._auto_skill_promotion_ruled_out() is False
    assert loader.set_pinned("auto/root-backed", True) is False
    retire_notices = [
        record
        for record in caplog.records
        if skills_mod._AUTHORITY_RETIRE_COMMAND in record.getMessage()
    ]
    assert len(retire_notices) == 1
    with pytest.raises(OSError) as refused:
        skills_mod.initialize_gateway_auto_skill_private_authority()
    assert skills_mod._AUTHORITY_RETIRE_COMMAND in str(refused.value)


def test_operator_retire_moves_verified_idle_authority_and_restores_by_name_mutation(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    skills_mod.initialize_auto_skill_private_authority(
        skills_root=loader._dir,
        data_home=loader._private_root().parents[1],
    )
    root = loader._private_root()
    record = loader._authority_provenance_path()
    root_identity = root.stat().st_ino
    record_bytes = record.read_bytes()
    _write_live(loader, "after-retire", version=1, body="OLD")
    skills_mod._reset_auto_skill_private_authority_for_tests()

    retired_root, retired_record, _history = skills_mod.retire_auto_skill_private_authority()

    assert not root.exists()
    assert not record.exists()
    assert retired_root.is_dir()
    assert retired_root.stat().st_ino == root_identity
    assert retired_record.read_bytes() == record_bytes
    assert retired_root.name.split(".stale-", 1)[1] == retired_record.name.split(".stale-", 1)[1]
    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        lambda: skills_mod._SANDBOX_OFF_REFUSAL,
    )
    assert skills_mod._auto_skill_promotion_ruled_out() is True
    assert loader.set_pinned("auto/after-retire", True) is True


def test_authority_retire_cli_holds_the_gateway_lock(monkeypatch, tmp_path, capsys):
    from kiro_crew import cli

    events: list[object] = []
    retired_root = tmp_path / "auto-skill-private.stale-token"
    retired_record = tmp_path / "auto-skill-private-authority.json.stale-token"
    retired_quarantine = tmp_path / ".quarantine.stale-token"

    class HeldGatewayLock:
        def __init__(self, home):
            events.append(("constructed", home))

        def __enter__(self):
            events.append("entered")
            return self

        def __exit__(self, *_exc):
            events.append("exited")

    def retire():
        events.append("retired")
        return retired_root, retired_record, (retired_quarantine,)

    monkeypatch.setattr(cli, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "GatewayLock", HeldGatewayLock)
    monkeypatch.setattr(cli, "retire_auto_skill_private_authority", retire)

    cli._skills_cmd(argparse.Namespace(skills_action="authority-retire"))

    assert events == [("constructed", tmp_path), "entered", "retired", "exited"]
    output = capsys.readouterr().out
    assert str(retired_root) in output
    assert str(retired_record) in output
    assert f"quarantine: {retired_quarantine}" in output
    assert "moved aside with the root" in output


def test_authority_retire_cli_reports_a_root_retired_without_a_record(
    monkeypatch,
    tmp_path,
    capsys,
):
    from kiro_crew import cli

    retired_root = tmp_path / "auto-skill-private.stale-token"
    monkeypatch.setattr(cli, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "GatewayLock", lambda _home: contextlib.nullcontext())
    monkeypatch.setattr(
        cli, "retire_auto_skill_private_authority", lambda: (retired_root, None, ())
    )

    cli._skills_cmd(argparse.Namespace(skills_action="authority-retire"))

    output = capsys.readouterr().out
    assert str(retired_root) in output
    assert "record: (none present)" in output
    assert "quarantine: (none present)" in output


# ── Retirement on a host where no process can promote ──


def _turn_the_host_sandbox_off(monkeypatch):
    """Model the operator turning ``agent.sandbox`` off: no process can certify."""
    from kiro_crew import sandbox

    skills_mod._reset_auto_skill_private_authority_for_tests()
    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        _REAL_AUTHORITY_SANDBOX_REFUSAL,
    )
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "off")
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda _mode: False)
    monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda _mode: "off")
    monkeypatch.setattr(sandbox, "unavailable_kind", lambda: "")
    assert skills_mod._auto_skill_sandbox_excludes_every_promoter() is True


def _stale_names(directory: Path) -> list[str]:
    return sorted(name for name in os.listdir(directory) if ".stale-" in name)


@pytest.mark.parametrize("case", ["home-moved", "key-regenerated", "record-missing"])
def test_maskless_retire_sets_aside_an_unverifiable_root_without_trusting_it(
    uninitialized_loader,
    monkeypatch,
    case,
):
    """A home moved by ``cp -a``, rsync or a restore onto a sandbox-off host keeps a
    root whose provenance never verifies again. Retirement there moves it aside
    untrusted, so a planted always-on skill becomes removable again."""
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    _initialize_test_authority(loader)
    planted = _plant_always_on_auto_skill(loader, "planted-always")
    root = loader._private_root()
    record = loader._authority_provenance_path()
    root_identity = root.stat().st_ino
    _rewrite_authority_record(loader, home, case)
    record_bytes = record.read_bytes() if record.exists() else None
    _turn_the_host_sandbox_off(monkeypatch)
    assert loader.delete_skill("auto/planted-always") is False

    retired_root, retired_record, _history = skills_mod.retire_auto_skill_private_authority()

    assert not os.path.lexists(root)
    assert not os.path.lexists(record)
    assert retired_root.stat().st_ino == root_identity
    token = retired_root.name.split(".stale-", 1)[1]
    if record_bytes is None:
        assert retired_record is None
        assert _stale_names(root.parent) == [retired_root.name]
    else:
        assert retired_record is not None
        assert retired_record.read_bytes() == record_bytes
        assert retired_record.name.split(".stale-", 1)[1] == token
    assert skills_mod._auto_skill_promotion_ruled_out() is True
    assert loader.delete_skill("auto/planted-always") is True
    assert not planted.exists()


@pytest.mark.parametrize("surface", ["evidence", "public-quarantine"])
def test_maskless_retire_ignores_retired_history_past_the_scan_bound(
    uninitialized_loader,
    monkeypatch,
    surface,
):
    """History that outgrew the stale-state scan refuses on a masked host and is
    never read on a host where no process can promote."""
    loader = uninitialized_loader
    _initialize_test_authority(loader)
    root = loader._private_root()
    history = (
        root / skills_mod.AUTO_EVIDENCE_DIRNAME
        if surface == "evidence"
        else loader._quarantine_root()
    )
    history.mkdir(parents=True, exist_ok=True)
    for index in range(skills_mod._STALE_CLAIM_SCAN_LIMIT + 1):
        (history / f"history-{index}--{index:032x}").mkdir()
    root_identity = root.stat().st_ino
    _write_live(loader, "after-history", version=1, body="OLD")
    skills_mod._reset_auto_skill_private_authority_for_tests()

    with pytest.raises(skills_mod._StaleClaimScanOverflow):
        skills_mod.retire_auto_skill_private_authority()
    assert root.stat().st_ino == root_identity
    assert _stale_names(root.parent) == []

    _turn_the_host_sandbox_off(monkeypatch)
    retired_root, retired_record, _history = skills_mod.retire_auto_skill_private_authority()

    assert not os.path.lexists(root)
    assert retired_root.stat().st_ino == root_identity
    assert retired_record is not None and retired_record.is_file()
    assert loader.set_pinned("auto/after-history", True) is True


def test_masked_host_retire_refuses_an_unverifiable_root_with_the_exact_handoff(
    uninitialized_loader,
):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    _initialize_test_authority(loader)
    root = loader._private_root()
    root_identity = root.stat().st_ino
    _rewrite_authority_record(loader, home, "home-moved")
    record_bytes = loader._authority_provenance_path().read_bytes()
    skills_mod._reset_auto_skill_private_authority_for_tests()
    assert skills_mod._auto_skill_sandbox_excludes_every_promoter() is False

    with pytest.raises(OSError) as refused:
        skills_mod.retire_auto_skill_private_authority()

    expected = SkillsLoader._authority_handoff(
        "authority provenance, selected-home identity, or root identity mismatches"
    )
    assert str(refused.value) == str(expected)
    assert root.stat().st_ino == root_identity
    assert loader._authority_provenance_path().read_bytes() == record_bytes
    assert _stale_names(root.parent) == []


def test_maskless_retire_refuses_a_claim_in_flight_and_names_the_manual_recovery(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    skills_mod.initialize_gateway_auto_skill_private_authority()
    loader.stage_skill_candidate(
        "in-flight",
        description="d",
        triggers="t",
        procedure_md="## Steps\n\nrun\n",
        provenance=_prov(),
    )
    claimed = loader._claim_pending_update("in-flight")
    assert claimed is not None
    claim, claim_fd, _consumed_at, _snapshot = claimed
    skills_mod.platform_compat.release_lock(claim_fd)
    os.close(claim_fd)
    root = loader._private_root()
    root_identity = root.stat().st_ino
    _rewrite_authority_record(loader, home, "key-regenerated")
    _turn_the_host_sandbox_off(monkeypatch)

    with pytest.raises(OSError) as refused:
        skills_mod.retire_auto_skill_private_authority()

    message = str(refused.value)
    assert "1 claim(s) in flight" in message
    assert claim.name in message
    assert "To recover by hand" in message
    assert "skills/auto/.quarantine/<slug>--<token>" in message
    assert root.stat().st_ino == root_identity
    assert (loader._claims_root() / claim.name).is_dir()
    assert _stale_names(root.parent) == []


def test_maskless_retire_refuses_when_active_claims_exceed_the_bound(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    _initialize_test_authority(loader)
    claims = loader._claims_root()
    claims.mkdir(parents=True)
    for index in range(4):
        (claims / f"active-{index}--{index:032x}").mkdir()
    monkeypatch.setattr(skills_mod, "_STALE_CLAIM_SCAN_LIMIT", 3)
    _turn_the_host_sandbox_off(monkeypatch)

    with pytest.raises(OSError, match="claims exceeded the 3-entry stale-claim scan limit"):
        skills_mod.retire_auto_skill_private_authority()

    assert loader._private_root().is_dir()
    assert _stale_names(loader._private_root().parent) == []


@pytest.mark.skipif(os.name == "nt", reason="simulated Windows rename lock runs on POSIX")
@pytest.mark.parametrize("host", ["masked", "maskless"])
def test_simulated_windows_retire_holds_no_pin_on_the_root_it_renames(
    uninitialized_loader,
    monkeypatch,
    host,
):
    """A Windows directory handle opened without ``FILE_SHARE_DELETE`` forbids
    renaming that directory or any directory above it."""
    loader = uninitialized_loader
    _simulate_windows_native_handles(monkeypatch)
    _initialize_test_authority(loader)
    root_key = os.path.realpath(loader._private_root())
    loader._quarantine_root().mkdir(parents=True, exist_ok=True)
    quarantine_key = os.path.realpath(loader._quarantine_root())
    held: dict[str, int] = {}
    real_parent_pin = SkillsLoader._pin_skill_parent
    real_child_pin = SkillsLoader._pin_skill_child_parent

    @contextlib.contextmanager
    def hold(pin):
        key = os.path.normpath(str(pin.path))
        held[key] = held.get(key, 0) + 1
        try:
            yield pin
        finally:
            held[key] -= 1

    @contextlib.contextmanager
    def tracked_parent_pin(self, path):
        with real_parent_pin(self, path) as pin, hold(pin) as held_pin:
            yield held_pin

    @contextlib.contextmanager
    def tracked_child_pin(self, parent, name, **kwargs):
        with real_child_pin(self, parent, name, **kwargs) as pin, hold(pin) as held_pin:
            yield held_pin

    real_rename = os.rename
    renamed: list[str] = []

    def windows_rename(source, destination, *args, **kwargs):
        source_key = os.path.normpath(os.fspath(source))
        for key, count in held.items():
            if count and (key == source_key or key.startswith(source_key + os.sep)):
                raise PermissionError(
                    errno.EACCES,
                    "a directory handle without FILE_SHARE_DELETE is open",
                    source_key,
                )
        renamed.append(source_key)
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(SkillsLoader, "_pin_skill_parent", tracked_parent_pin)
    monkeypatch.setattr(SkillsLoader, "_pin_skill_child_parent", tracked_child_pin)
    monkeypatch.setattr(os, "rename", windows_rename)
    if host == "maskless":
        monkeypatch.setattr(skills_mod, "_auto_skill_sandbox_excludes_every_promoter", lambda: True)
    skills_mod._reset_auto_skill_private_authority_for_tests()

    retired_root, retired_record, _history = skills_mod.retire_auto_skill_private_authority()

    assert root_key in renamed
    assert quarantine_key in renamed
    assert not os.path.lexists(root_key)
    assert retired_root.is_dir()
    assert retired_record is not None and retired_record.is_file()


# ── Claim history never exhausts the active-claim bound ──


def _plant_consumed_quarantine_history(loader, count):
    """Model ``count`` lifetime claims: a public quarantine name retired into evidence."""
    assert loader._private_state_roots_safe(create=True) is True
    for index in range(count):
        name = f"history-{index}--{index:032x}"
        (loader._quarantine_root() / name).mkdir()
        (loader._evidence_root() / name).mkdir()


def _stage_and_abandon_claim(loader, slug):
    loader.stage_skill_candidate(
        slug,
        description="d",
        triggers="t",
        procedure_md="## Steps\n\nrun\n",
        provenance=_prov(),
    )
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, claim_fd, _consumed_at, _snapshot = claimed
    # The owning process dies mid-claim: its lock is released, its claim stays.
    skills_mod.platform_compat.release_lock(claim_fd)
    os.close(claim_fd)
    assert not (loader._pending_root() / slug).exists()
    return claim


def test_consumed_quarantine_history_never_exhausts_the_active_claim_bound(loader):
    _plant_consumed_quarantine_history(loader, skills_mod._ACTIVE_CLAIM_SCAN_LIMIT + 1)

    assert loader._pending_slug_claimed("after-history") is False
    assert (
        loader.stage_skill_candidate(
            "after-history",
            description="d",
            triggers="t",
            procedure_md="## Steps\n\nrun\n",
            provenance=_prov(),
        )
        is not None
    )
    claim = _stage_and_abandon_claim(loader, "interrupted")

    loader._recover_abandoned_claims()

    assert (loader._pending_root() / "interrupted" / "SKILL.md").is_file()
    assert not claim.exists()


def test_unconsumed_quarantine_names_still_bound_the_active_claim_scan(loader, caplog):
    claim = _stage_and_abandon_claim(loader, "interrupted")
    for index in range(skills_mod._ACTIVE_CLAIM_SCAN_LIMIT + 1):
        (loader._quarantine_root() / f"planted-{index}--{index:032x}").mkdir()

    assert loader._pending_slug_claimed("after-plant") is None
    with caplog.at_level("WARNING", logger="kiro_crew.skills"):
        loader._recover_abandoned_claims()

    assert "active claim namespace exceeded the" in caplog.text
    assert claim.is_dir()
    assert not (loader._pending_root() / "interrupted").exists()


def _turn_the_host_sandbox_back_on(monkeypatch):
    """Model the operator re-enabling ``agent.sandbox``: the gateway certifies again."""
    monkeypatch.setattr(skills_mod, "_auto_skill_authority_sandbox_refusal", lambda: None)
    monkeypatch.setattr(skills_mod, "_auto_skill_sandbox_excludes_every_promoter", lambda: False)


@pytest.mark.parametrize("host", ["masked", "maskless"])
def test_retire_sets_quarantine_history_aside_so_a_fresh_root_stages_again(
    uninitialized_loader,
    monkeypatch,
    host,
):
    """Private ``evidence/`` is what makes a public quarantine name history, and it
    leaves with the retired root. Left behind, every public name would count as an
    active claim against the next fresh root and refuse staging and recovery for
    good, so both public quarantines go aside under the root's token."""
    loader = uninitialized_loader
    if host == "masked":
        # The verified path refuses history past the stale-state scan bound, so a
        # smaller active bound reproduces the same overflow below it.
        monkeypatch.setattr(skills_mod, "_ACTIVE_CLAIM_SCAN_LIMIT", 3)
    skills_mod.initialize_gateway_auto_skill_private_authority()
    history_count = skills_mod._ACTIVE_CLAIM_SCAN_LIMIT + 1
    _plant_consumed_quarantine_history(loader, history_count)
    live_history = loader._live_quarantine_root() / f"live-history--{0:032x}"
    live_history.mkdir()
    (loader._evidence_root() / live_history.name).mkdir()
    auto = loader._quarantine_root().parent
    if host == "maskless":
        _turn_the_host_sandbox_off(monkeypatch)
    else:
        monkeypatch.setattr(
            skills_mod, "_auto_skill_sandbox_excludes_every_promoter", lambda: False
        )
        skills_mod._reset_auto_skill_private_authority_for_tests()

    retired_root, _record, retired_history = skills_mod.retire_auto_skill_private_authority()

    token = retired_root.name.split(".stale-", 1)[1]
    aside = {path.name: path for path in retired_history}
    quarantine_name = f"{skills_mod.AUTO_QUARANTINE_DIRNAME}.stale-{token}"
    live_quarantine_name = f"{skills_mod.AUTO_LIVE_QUARANTINE_DIRNAME}.stale-{token}"
    assert sorted(aside) == sorted([quarantine_name, live_quarantine_name])
    assert all(os.path.samefile(path.parent, auto) for path in retired_history)
    assert not os.path.lexists(loader._quarantine_root())
    assert not os.path.lexists(loader._live_quarantine_root())
    # Nothing is deleted: the history waits beside its evidence for inspection.
    assert len(os.listdir(aside[quarantine_name])) == history_count
    assert (aside[live_quarantine_name] / live_history.name).is_dir()
    evidence = os.listdir(retired_root / skills_mod.AUTO_EVIDENCE_DIRNAME)
    assert len(evidence) == history_count + 1

    _turn_the_host_sandbox_back_on(monkeypatch)
    _restart_gateway_certification()

    assert (
        loader.stage_skill_candidate(
            "after-retire",
            description="d",
            triggers="t",
            procedure_md="## Steps\n\nrun\n",
            provenance=_prov(),
        )
        is not None
    )
    assert loader._pending_slug_claimed("another-slug") is False
    claim = _stage_and_abandon_claim(loader, "interrupted")

    loader._recover_abandoned_claims()

    assert (loader._pending_root() / "interrupted" / "SKILL.md").is_file()
    assert not claim.exists()


def test_a_failed_quarantine_set_aside_restores_every_retired_entry(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    _initialize_test_authority(loader)
    _plant_consumed_quarantine_history(loader, 2)
    loader._live_quarantine_root().mkdir(exist_ok=True)
    entries = [
        loader._private_root(),
        loader._authority_provenance_path(),
        loader._quarantine_root(),
        loader._live_quarantine_root(),
    ]
    identities = [entry.stat().st_ino for entry in entries]
    real_rename = SkillsLoader._rename_skill_child_no_replace

    def refuse_the_last_set_aside(self, source, source_name, *args, **kwargs):
        if source_name == skills_mod.AUTO_LIVE_QUARANTINE_DIRNAME:
            raise OSError(errno.EIO, "simulated rename failure")
        return real_rename(self, source, source_name, *args, **kwargs)

    monkeypatch.setattr(SkillsLoader, "_rename_skill_child_no_replace", refuse_the_last_set_aside)
    monkeypatch.setattr(skills_mod, "_auto_skill_sandbox_excludes_every_promoter", lambda: False)
    skills_mod._reset_auto_skill_private_authority_for_tests()

    with pytest.raises(OSError, match="simulated rename failure"):
        skills_mod.retire_auto_skill_private_authority()

    assert [entry.stat().st_ino for entry in entries] == identities
    assert _stale_names(loader._private_root().parent) == []
    assert _stale_names(loader._quarantine_root().parent) == []


# ── Only an unmasked boundary can prove authority absence ──


def _hide_authority_behind_empty_mask(loader):
    """Replace the process view with the empty directory a sandbox mask exposes."""
    home = loader._private_root().parents[1]
    host_tag_grants = home / "tag-grants-host-view"
    (home / skills_mod._AUTHORITY_PROVENANCE_PARENT).rename(host_tag_grants)
    (home / skills_mod._AUTHORITY_PROVENANCE_PARENT).mkdir()
    skills_mod._reset_auto_skill_private_authority_for_tests()
    return host_tag_grants / skills_mod.AUTO_SKILL_PRIVATE_STATE_DIRNAME


@pytest.mark.parametrize("confinement", ["marker", "seatbelt"])
def test_masked_authority_view_never_proves_real_root_absent(
    uninitialized_loader,
    monkeypatch,
    caplog,
    confinement,
):
    """An empty masked ``tag-grants`` view cannot authorize by-name mutation."""
    from kiro_crew import sandbox

    loader = uninitialized_loader
    skills_mod.initialize_auto_skill_private_authority(
        skills_root=loader._dir,
        data_home=loader._private_root().parents[1],
    )
    real_root = _hide_authority_behind_empty_mask(loader)
    if confinement == "marker":
        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        monkeypatch.setattr(sandbox, "_macos_sandbox_state", lambda: None)
    else:
        monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
        monkeypatch.setattr(sandbox, "_macos_sandbox_state", lambda: True)
    live = _write_live(loader, "masked-real-root", version=1, body="OLD")
    before = (live / "SKILL.md").read_bytes()

    caplog.set_level(logging.WARNING, logger="kiro_crew.skills")
    assert real_root.is_dir()
    assert not loader._private_root().exists()
    assert skills_mod._auto_skill_authority_root_exists() is True
    assert skills_mod._auto_skill_promotion_ruled_out() is False
    assert loader.set_pinned("auto/masked-real-root", True) is False

    assert (live / "SKILL.md").read_bytes() == before
    assert "cannot be observed from inside the agent sandbox" in caplog.text


def test_unmasked_root_absence_still_grants_by_name_mutation(
    uninitialized_loader,
    monkeypatch,
):
    """The r4 rootless behavior remains available to an operator-side process."""
    from kiro_crew import sandbox

    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    monkeypatch.setattr(sandbox, "_macos_sandbox_state", lambda: False)
    loader = uninitialized_loader
    live = _write_live(loader, "unmasked-rootless", version=1, body="OLD")
    before = (live / "SKILL.md").read_bytes()

    assert not loader._private_root().exists()
    assert skills_mod._auto_skill_authority_root_exists() is False
    assert skills_mod._auto_skill_promotion_ruled_out() is True
    assert loader.set_pinned("auto/unmasked-rootless", True) is True
    assert (live / "SKILL.md").read_bytes() != before


def test_in_sandbox_root_absence_and_fresh_no_replace_probe_both_refuse_mutation(
    uninitialized_loader,
    monkeypatch,
    caplog,
):
    """Neither masked absence nor a masked fresh probe can grant mutation."""
    from kiro_crew import sandbox

    monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
    monkeypatch.setattr(sandbox, "_macos_sandbox_state", lambda: None)
    monkeypatch.setattr(
        skills_mod,
        "_STARTUP_AUTHORITY_REFUSAL",
        skills_mod._NO_REPLACE_RENAME_REFUSAL,
    )

    def unsupported_probe(*_args, **_kwargs):
        raise NotImplementedError

    with monkeypatch.context() as fresh_probe:
        fresh_probe.setattr(
            SkillsLoader,
            "_ensure_private_authority",
            lambda *_args, **_kwargs: object(),
        )
        fresh_probe.setattr(
            SkillsLoader,
            "_probe_rename_under_authority",
            unsupported_probe,
        )
        assert skills_mod._no_replace_refusal_still_binds_data_home() is False

    loader = uninitialized_loader
    live = _write_live(loader, "sandbox-rootless", version=1, body="OLD")
    before = (live / "SKILL.md").read_bytes()
    caplog.set_level(logging.WARNING, logger="kiro_crew.skills")

    assert not loader._private_root().exists()
    assert skills_mod._auto_skill_authority_root_exists() is True
    assert skills_mod._auto_skill_promotion_ruled_out() is False
    assert loader.set_pinned("auto/sandbox-rootless", True) is False

    assert (live / "SKILL.md").read_bytes() == before
    assert "cannot be observed from inside the agent sandbox" in caplog.text


# ── Unmasked root absence grants by-name mutation ──


@pytest.mark.parametrize(
    ("mode", "effective", "kind", "host_wide"),
    [
        ("off", "off", "", True),
        ("standard", "standard", "no_backend", True),
        ("standard", "standard", "transient", False),
        ("standard", "standard", "foreign_sandbox", False),
        ("off", "standard", "", False),
    ],
    ids=[
        "sandbox-off",
        "no-backend",
        "transient-probe",
        "foreign-outer-sandbox",
        "floor-raises-off",
    ],
)
def test_maskless_host_predicates_shape_only_the_retire_hint(
    uninitialized_loader,
    monkeypatch,
    mode,
    effective,
    kind,
    host_wide,
):
    from kiro_crew import sandbox

    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        _REAL_AUTHORITY_SANDBOX_REFUSAL,
    )
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: mode)
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda _mode: False)
    monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda _mode: effective)
    monkeypatch.setattr(sandbox, "unavailable_kind", lambda: kind)

    with pytest.raises(OSError):
        skills_mod.initialize_gateway_auto_skill_private_authority()

    assert skills_mod._STARTUP_AUTHORITY_BINDING is None
    assert skills_mod._auto_skill_sandbox_excludes_every_promoter() is host_wide
    assert skills_mod._auto_skill_promotion_ruled_out() is True
    _write_live(uninitialized_loader, "maskless-live", version=1, body="OLD")
    assert uninitialized_loader.set_pinned("auto/maskless-live", True) is True
    assert not _authority_path(uninitialized_loader._private_root().parents[1]).exists()


def test_an_unreadable_sandbox_predicate_does_not_override_root_absence(
    uninitialized_loader,
    monkeypatch,
):
    from kiro_crew import sandbox

    def unreadable(_mode):
        raise RuntimeError("governance floor could not be read")

    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        lambda: skills_mod._SANDBOX_MASK_UNAVAILABLE_REFUSAL,
    )
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    monkeypatch.setattr(sandbox, "effective_sandbox_mode", unreadable)
    monkeypatch.setattr(sandbox, "unavailable_kind", lambda: "no_backend")

    assert skills_mod._auto_skill_sandbox_excludes_every_promoter() is False
    assert skills_mod._auto_skill_promotion_ruled_out() is True
    _write_live(uninitialized_loader, "unreadable-rootless", version=1, body="OLD")
    assert uninitialized_loader.set_pinned("auto/unreadable-rootless", True) is True


def test_sandbox_off_keeps_live_deletion_and_explicit_dismissal(uninitialized_loader, monkeypatch):
    from kiro_crew import sandbox

    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        _REAL_AUTHORITY_SANDBOX_REFUSAL,
    )
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "off")
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: mode != "off")
    monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda mode: mode)
    loader = uninitialized_loader

    with pytest.raises(OSError, match="sandbox_off"):
        skills_mod.initialize_gateway_auto_skill_private_authority()

    planted = _plant_always_on_auto_skill(loader, "maskless-always")
    assert loader.set_pinned("auto/maskless-always", True) is True
    assert loader.delete_skill("auto/maskless-always") is True
    assert not planted.exists()
    pending = loader._pending_root() / "queued-maskless"
    pending.mkdir(parents=True)
    (pending / "SKILL.md").write_text("## Steps\n\nqueued\n", encoding="utf-8")
    assert loader.dismiss_pending_skill("queued-maskless") is True
    assert not pending.exists()
    assert not _authority_path(loader._private_root().parents[1]).exists()


def test_root_absence_keeps_by_name_mutation_across_sandbox_mode_changes(
    uninitialized_loader,
    monkeypatch,
):
    """A mode change cannot create promotion authority while the root is absent."""
    from kiro_crew import sandbox

    mode = "off"
    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        _REAL_AUTHORITY_SANDBOX_REFUSAL,
    )
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: mode)
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda current: current != "off")
    monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda current: current)
    with pytest.raises(OSError, match="sandbox_off"):
        skills_mod.initialize_gateway_auto_skill_private_authority()
    _write_live(uninitialized_loader, "maskless-flip", version=1, body="OLD")
    assert skills_mod._auto_skill_promotion_ruled_out() is True
    assert uninitialized_loader.set_pinned("auto/maskless-flip", True) is True

    mode = "standard"

    assert skills_mod._auto_skill_promotion_ruled_out() is True
    assert uninitialized_loader.set_pinned("auto/maskless-flip", False) is True
    assert not _authority_path(uninitialized_loader._private_root().parents[1]).exists()


# ── A rename probe or one stuck claim never freezes lifecycle ──


def _plant_rename_probe(loader, *, orphaned):
    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is True
    probe = loader._claims_root() / (
        f"{skills_mod._RENAME_PROBE_PREFIX}{secrets.token_hex(16)}-source"
    )
    probe.write_bytes(b"")
    # Set the modification time rather than wait for one: an orphan is older than
    # the cleanup threshold, a probe still in use is not.
    stamp = 1_000_000.0 if orphaned else datetime.now(tz=timezone.utc).timestamp() + 3600
    os.utime(probe, (stamp, stamp))
    return probe


_FAR_FUTURE_LIFECYCLE = {
    "max_auto_skills": 100,
    "stale_after_days": 1,
    "archive_after_days": 1,
    "now": datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp(),
}


def test_an_orphaned_rename_probe_neither_freezes_lifecycle_nor_survives_recovery(loader):
    live = _write_live(loader, "probe-unrelated", version=1, body="OLD")
    orphan = _plant_rename_probe(loader, orphaned=True)
    in_use = _plant_rename_probe(loader, orphaned=False)

    result = loader.run_skill_lifecycle(**_FAR_FUTURE_LIFECYCLE)

    assert result["archived"] == 1
    assert not live.exists()
    loader._recover_abandoned_claims()
    assert not orphan.exists()
    assert in_use.exists()


def test_recovery_removes_at_most_the_bounded_number_of_orphaned_probes(loader, monkeypatch):
    monkeypatch.setattr(skills_mod, "_ORPHANED_RENAME_PROBE_CLEANUP_LIMIT", 2)
    probes = [_plant_rename_probe(loader, orphaned=True) for _ in range(3)]

    loader._recover_abandoned_claims()
    assert sum(probe.exists() for probe in probes) == 1
    loader._recover_abandoned_claims()
    assert not any(probe.exists() for probe in probes)


def test_an_unreadable_valid_claim_journal_exempts_every_lifecycle_target(
    loader,
    monkeypatch,
    caplog,
):
    victim = _write_live(loader, "opaque-victim", version=1, body="OLD")
    unrelated = _write_live(loader, "opaque-unrelated", version=1, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        "opaque-candidate",
        target="auto/opaque-victim",
        body="## Steps\n\nNEW",
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )
    with monkeypatch.context() as interrupted:
        interrupted.setattr(loader, "_commit_claim_consumption", lambda *_args: False)
        assert (
            loader.auto_apply_pending_update(
                "opaque-candidate",
                expected_candidate_binding=binding[0],
            )
            is None
        )
    (claim,) = list(loader._claims_root().glob("opaque-candidate--*"))
    lock_path = loader._claim_lock_path(claim.name)
    real_open = loader._open_skill_lock

    def unreadable(parent, name, **kwargs):
        if parent.path == lock_path.parent and name == lock_path.name:
            raise OSError("claim journal is unreadable")
        return real_open(parent, name, **kwargs)

    monkeypatch.setattr(loader, "_open_skill_lock", unreadable)
    caplog.set_level(logging.WARNING, logger="kiro_crew.skill_runtime.auto_skills")

    result = loader.run_skill_lifecycle(**_FAR_FUTURE_LIFECYCLE)

    assert result["archived"] == 0
    assert victim.is_dir()
    assert unrelated.is_dir()
    messages = [record.getMessage() for record in caplog.records]
    assert any(str(lock_path) in message for message in messages)
    assert any("restart the gateway to recover it" in message for message in messages)


def test_a_readable_authenticated_journal_exempts_only_its_recorded_target(
    loader,
    monkeypatch,
):
    victim = _write_live(loader, "readable-victim", version=1, body="OLD")
    unrelated = _write_live(loader, "readable-unrelated", version=1, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        "opaque-candidate",
        target="auto/readable-victim",
        body="## Steps\n\nNEW",
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )
    with monkeypatch.context() as interrupted:
        interrupted.setattr(loader, "_commit_claim_consumption", lambda *_args: False)
        assert (
            loader.auto_apply_pending_update(
                "opaque-candidate",
                expected_candidate_binding=binding[0],
            )
            is None
        )

    result = loader.run_skill_lifecycle(**_FAR_FUTURE_LIFECYCLE)

    assert result["archived"] == 1
    assert victim.is_dir()
    assert not unrelated.exists()


def test_a_journal_in_a_pre_publication_state_exempts_no_target(loader):
    """Recovery never reconciles a live generation for an active-state journal, so
    neither its own slug nor any other target is held live by it."""
    own = _write_live(loader, "journal-active", version=1, body="OLD")
    other = _write_live(loader, "journal-other", version=1, body="OLD")
    claim_name = f"journal-active--{secrets.token_hex(16)}"
    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is True
    (loader._claims_root() / claim_name).mkdir()
    _plant_claim_lock(loader, claim_name)

    result = loader.run_skill_lifecycle(**_FAR_FUTURE_LIFECYCLE)

    assert result["archived"] == 2
    assert not own.exists()
    assert not other.exists()


def test_a_crash_orphaned_rename_probe_does_not_block_a_stale_reseed(uninitialized_loader):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    skills_mod.initialize_gateway_auto_skill_private_authority()
    _plant_rename_probe(loader, orphaned=False)
    _rewrite_authority_record(loader, home, "record-missing")

    binding = _restart_gateway_certification()

    assert binding == skills_mod._STARTUP_AUTHORITY_BINDING
    assert skills_mod.auto_skill_promotion_disabled_reason() is None
