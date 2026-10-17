"""Helpers, shared constants and the skipif mark for the skill-versioning suite.

Imported by name from each ``test_skill_versioning*`` module. The constants are
captured at import, before any fixture patches the attributes they read.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

import kiro_crew.skills as skills_mod
from kiro_crew.skills import AutoSkillProvenance, canonical_skill_text_hash

#: Lost-run bound for a wait a test's own thread must release (testing
#: conventions D8): generous, because it only matters when the release never comes.
_THREAD_WAIT_CEILING_SECS = 30.0

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


def _linked_pending_candidate(loader, slug):
    victim = loader._pending_root() / f"{slug}-victim"
    victim.mkdir(parents=True, exist_ok=True)
    (victim / "SKILL.md").write_text("VICTIM", encoding="utf-8")
    metadata = '{"name": "victim", "notify_suppressed": true}'
    (victim / ".meta.json").write_text(metadata, encoding="utf-8")
    link = loader._pending_root() / slug
    skills_mod.platform_compat.symlink_or_junction(victim, link)
    return link, victim, metadata
