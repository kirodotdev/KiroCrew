"""Unattended auto-apply promotion and the atomic single-slug claim.

Covers ``auto_apply`` binding integrity (exact staged bytes, CRLF/LF base-hash
equivalence, refusal of a body or scripts mutated after the binding check), the
shared promotion lock and canonical-namespace guard, and the claim-capture
snapshot that isolates each flow from a transient pending-path replacement.
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
from kiro_crew.skill_runtime import authoring as skill_authoring
from kiro_crew.skill_runtime import auto_skills as skill_auto
from kiro_crew.skills import SkillsLoader, canonical_skill_text_hash

from .skill_versioning_support import (
    _THREAD_WAIT_CEILING_SECS,
    _initialize_test_authority,
    _open_lock_for_test,
    _prov,
    _stage_update,
    _write_live,
)

# This module's auto-skill boundaries initialize explicitly through the
# ``loader`` fixture; every ``uninitialized_loader`` path must see production
# fail closed.
pytestmark = pytest.mark.usefixtures("no_auto_skill_authority_startup")


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
        assert loader.approve_pending_update_checked(slug) == f"auto/{target}"
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

    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        loader.approve_pending_update_checked(slug)
    assert refused.value.reason == "promotion_failed"
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

    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        loader.approve_pending_update_checked(slug)
    assert refused.value.reason == "promotion_failed"
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

    assert loader.approve_pending_update_checked(slug) == f"auto/{target}"
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
        assert loader.approve_pending_update_checked(slug) == f"auto/{target}"
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
        assert loader.approve_pending_update_checked(slug) == "auto/retained-first"
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

    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        loader.approve_pending_update_checked(slug)
    assert refused.value.reason == "promotion_failed"
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
        try:
            return loader.approve_pending_update_checked(slug)
        except skills_mod.PendingApprovalRefused as refused:
            return refused.reason

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(promote, ["serialized-a", "serialized-b"]))

    assert results.count("auto/serialized") == 1
    assert results.count("stale_base") == 1
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
        approval = pool.submit(loader.approve_pending_skill_checked, "approve-replacement-race")
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
