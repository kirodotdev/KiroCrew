"""Importing a pack must not be able to lose art or write outside its directory.

Two findings from review, both in the import/overwrite path:

  * `_safe_filename` rejected separators with a denylist, so `C:evil.json` passed —
    no `/`, no `\\`, no `..` — and on Windows `Path("packs/x") / "C:evil.json"`
    resolves to `C:evil.json`, outside the pack entirely.
  * overwriting a pack ran `rmtree(target)` and THEN `os.replace(staging, target)`.
    Between those two calls the pack does not exist; a failed rename or a gateway
    exit there loses the user's custom art with nothing to restore from.
"""

from __future__ import annotations

import json
import os

import pytest

from kiro_crew.apps.builtins.crew_companion import appearances as ap


class TestFilenameValidation:
    @pytest.mark.parametrize(
        "name",
        [
            "C:evil.json",           # drive prefix — the reported escape
            "C:/evil.json",
            "manifest.json:stream",  # NTFS alternate data stream
            "../evil.svg",
            "/etc/passwd",
            "sub\\dir.svg",
            ".hidden",
            "star*.svg",
            "quest?.svg",
            "pipe|.svg",
            "nul\x00.svg",
            "",
            "   ",
            "x" * 129,
        ],
    )
    def test_a_name_that_could_escape_is_refused(self, name):
        assert ap._safe_filename(name) is None, f"accepted {name!r}"

    @pytest.mark.parametrize(
        "name",
        ["idle.svg", "manifest.json", "random-happy.png", "sleep_mask.svg", "a1.svg"],
    )
    def test_the_names_real_packs_use_are_accepted(self, name):
        # The guard must not be so strict that a legitimate pack stops importing.
        assert ap._safe_filename(name) == name


def _manifest(pack_id: str) -> dict:
    return {
        "meta": {"id": pack_id, "format": "svg", "type": "custom"},
        "states": {"idle": "idle.svg"},
    }


def _save(store, pack_id: str, marker: str) -> bool:
    return store.save_pack(
        pack_id, _manifest(pack_id), {"idle.svg": f"<svg data-marker='{marker}'/>"}
    )


class TestOverwriteKeepsTheOldPackUntilTheNewOneLands:
    def test_a_successful_overwrite_replaces_the_art(self, tmp_path):
        store = ap.AppearanceStore(tmp_path)
        assert _save(store, "mine", "first")
        assert _save(store, "mine", "second")

        art = (tmp_path / ap.PACKS_DIRNAME / "mine" / "idle.svg").read_text()
        assert "second" in art

    def test_a_failed_overwrite_leaves_the_original_intact(self, tmp_path, monkeypatch):
        """The data-loss case. Before the fix the pack was already deleted by here."""
        store = ap.AppearanceStore(tmp_path)
        assert _save(store, "mine", "first")
        art = tmp_path / ap.PACKS_DIRNAME / "mine" / "idle.svg"
        original = art.read_text()

        real_replace = os.replace
        calls = {"n": 0}

        def fail_on_the_commit(src, dst, *a, **kw):
            # let the "move the old pack aside" rename through, break the commit
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(5, "Input/output error")
            return real_replace(src, dst, *a, **kw)

        monkeypatch.setattr(os, "replace", fail_on_the_commit)
        _save(store, "mine", "second")
        monkeypatch.undo()

        assert art.exists(), "the pack was lost"
        assert art.read_text() == original

    def test_a_leftover_backup_is_not_listed_as_a_pack(self, tmp_path):
        store = ap.AppearanceStore(tmp_path)
        assert _save(store, "mine", "first")

        # simulate a process death after the commit but before the cleanup
        leftover = tmp_path / ap.PACKS_DIRNAME / f"mine.old.{os.getpid()}"
        leftover.mkdir()
        (leftover / "manifest.json").write_text(json.dumps(_manifest("mine")), "utf-8")

        ids = [p["id"] for p in store.list_packs()]
        assert ids.count("mine") == 1, f"backup surfaced as a duplicate: {ids}"


class TestLinkedPackDeleteIsRefused:
    def test_deleting_a_symlink_alias_does_not_destroy_the_target(self, tmp_path):
        """A symlink named like a pack and pointing at a SIBLING pack resolves
        to that sibling, passes the containment check, and the recursive
        delete then destroys the victim's artwork. The alias must be refused,
        and the victim must survive with its art intact."""
        store = ap.AppearanceStore(tmp_path)
        store.load()
        assert store.save_pack(
            "victim",
            {"meta": {"id": "victim"}, "states": {"idle": "idle.svg"}},
            {"idle.svg": "<svg id='precious'/>"},
        )
        alias = tmp_path / ap.PACKS_DIRNAME / "alias"
        try:
            os.symlink(tmp_path / ap.PACKS_DIRNAME / "victim", alias)
        except OSError:
            pytest.skip("symlinks unavailable on this platform/user")

        assert store.delete_pack("alias") is False
        detail = store.pack_detail("victim")
        assert detail is not None
        assert "precious" in json.dumps(detail)


class TestOrphanedBackupRecovery:
    def test_a_stranded_backup_is_restored_on_load(self, tmp_path):
        """The overwrite is two renames (target->backup, staging->target).
        Dying between them leaves the pack existing ONLY as `<name>.old.<pid>`
        — which the listing filters out, so the art sits on disk while the
        gallery shows the pack as gone. Load must put it back."""
        store = ap.AppearanceStore(tmp_path)
        store.load()
        assert store.save_pack(
            "mine",
            {"meta": {"id": "mine", "name": "Mine"}, "states": {"idle": "idle.svg"}},
            {"idle.svg": "<svg id='survivor'/>"},
        )
        packs = tmp_path / ap.PACKS_DIRNAME
        # Simulate the crash window: target renamed aside, new one never landed.
        os.replace(packs / "mine", packs / "mine.old.12345")

        again = ap.AppearanceStore(tmp_path)
        again.load()
        detail = again.pack_detail("mine")
        assert detail is not None
        assert "survivor" in json.dumps(detail)
        assert not (packs / "mine.old.12345").exists()

    def test_a_stale_backup_next_to_a_live_pack_is_cleaned_up(self, tmp_path):
        """The opposite window (died after the second rename, before cleanup):
        the new pack won, the backup is garbage — remove it, keep the pack."""
        store = ap.AppearanceStore(tmp_path)
        store.load()
        assert store.save_pack(
            "mine",
            {"meta": {"id": "mine"}, "states": {"idle": "idle.svg"}},
            {"idle.svg": "<svg id='current'/>"},
        )
        packs = tmp_path / ap.PACKS_DIRNAME
        stale = packs / "mine.old.99999"
        stale.mkdir()
        (stale / "manifest.json").write_text("{}", "utf-8")

        again = ap.AppearanceStore(tmp_path)
        again.load()
        assert not stale.exists()
        detail = again.pack_detail("mine")
        assert detail is not None
        assert "current" in json.dumps(detail)


class TestLinkedPackFileReadIsRefused:
    def test_a_symlinked_pack_file_does_not_leak_outside_content(self, tmp_path):
        """A symlink named like a pack file resolves to ANY readable file on
        disk, and the detail endpoint would inline its contents to the
        frontend — an exfiltration channel through the avatar gallery. The
        read must refuse links; the rest of the pack still loads."""
        store = ap.AppearanceStore(tmp_path)
        store.load()
        assert store.save_pack(
            "leaky",
            {
                "meta": {"id": "leaky"},
                "states": {"idle": "idle.svg", "walk": "walk.svg"},
            },
            {"idle.svg": "<svg id='fine'/>", "walk.svg": "<svg id='fine2'/>"},
        )
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP-SECRET-CONTENT", "utf-8")
        pack_dir = tmp_path / ap.PACKS_DIRNAME / "leaky"
        (pack_dir / "walk.svg").unlink()
        try:
            os.symlink(secret, pack_dir / "walk.svg")
        except OSError:
            pytest.skip("symlinks unavailable on this platform/user")

        detail = store.pack_detail("leaky")
        assert detail is not None
        dumped = json.dumps(detail)
        assert "TOP-SECRET-CONTENT" not in dumped
        # The honest slot still renders.
        assert "fine" in dumped
