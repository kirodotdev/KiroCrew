"""Teardown, the settle hand-back, re-seeds and restores beside a sibling that shares a seed.

A live sharer holds the bytes it validated, so every owner path that revokes, rewrites or
moves the seed aside stands down for it or puts the file back exactly as found. A refused
hand-back drops governance and leaves the retry to teardown, a live sharer keeps the seed
and blocks adoption, a re-seed under a sharer keeps the validated bytes, and a share waits
for a settle whose restore never clobbers. Shared fixtures and helpers live in
``acp_seed_provenance_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path

from acp_seed_provenance_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_seed_provenance_helpers import isolated_records  # noqa: F401
from acp_seed_provenance_helpers import (
    _OWNER,
    _SERVED,
    _break_the_lock_when,
    _client,
    _fp,
    _seed,
    _settings,
    _teardown,
    _the_owning_process_died,
    _unwritable_sidecar,
)

from kiro_crew import model_registry as mr
from kiro_crew.acp import client as acp_client
from kiro_crew.acp import seed_provenance as sp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE


class TestSharedPermissionSurfaceTeardownAndReseed:
    """Teardown, the settle hand-back, re-seeds and restores around a seed a sibling shares.

    A sharer's teardown leaves the owner running and the owner's leaves the seed to a live
    sharer; a refused hand-back drops governance and leaves the retry to teardown; a live
    sharer blocks adoption; a re-seed under a sharer keeps the validated bytes; and a share
    waits for a settle whose restore never clobbers.
    """

    def test_sharer_teardown_leaves_the_owner_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        before = path.read_text(encoding="utf-8")
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert sibling._claude_settings_shared is True

        _teardown(sibling)

        # The file the owner is running against survives, the owner's live claim
        # survives, and the sharer's own governed state ends with its session.
        assert path.read_text(encoding="utf-8") == before
        assert sp._LIVE[os.fspath(path)] == owner._seed_owner
        assert owner._claude_settings_is_still_ours() is True
        assert sibling._claude_settings_shared is False
        assert sibling._permission_surface_governed is False

    def test_refused_durable_unshare_keeps_client_state_and_persisted_lease(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert sibling._claude_settings_shared is True

        monkeypatch.setattr(sp, "_persist", lambda **_kwargs: False)
        asyncio.run(sibling._discard_claude_settings_seed())

        assert sibling._claude_settings_shared is True
        assert sibling._seed_owner not in sp._SHARERS[os.fspath(path)]
        persisted = sp._read_disk_seeds()[os.fspath(path)]["holders"]["sharers"]
        assert sibling._seed_owner in persisted

    def test_owner_teardown_is_unchanged_once_sharers_are_gone(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert sibling._claude_settings_shared is True
        _teardown(sibling)

        _teardown(owner)

        # With no live sharer left, the owner still removes its own seed and
        # revokes its grant exactly as it did before sharers existed.
        assert not _settings(tmp_path).exists()
        assert sp.recorded(_settings(tmp_path), "a-later-session") is None

    def test_refused_settle_release_retains_the_live_owner(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        persisted_before = sp._read_disk_seeds()[os.fspath(path)]["holders"]

        monkeypatch.setattr(sp, "_persist", lambda **_kwargs: False)
        owner._settle_claude_settings_seed(
            path,
            owner._seed_owner,
            owner._claude_settings_written,
            owner._expected_settings_fingerprint(),
        )

        assert os.fspath(path) not in sp._LIVE
        assert sp._read_disk_seeds()[os.fspath(path)]["holders"] == persisted_before
        assert path.exists()

    def test_settle_releases_the_owner_holder_when_the_seed_was_removed(
        self, tmp_path, monkeypatch
    ):
        """A seed removed before teardown still hands back the durable owner holder.

        ``record()`` publishes this process's live identity as the path's durable
        owner. When the seed is gone at teardown (a ``git clean``, an ``rm``) the
        settle transaction has no file to move, revoke or delete -- but the owner
        holder is still Crew's to withdraw. Left standing, it names a live process,
        so no prune reclaims it and every later session in this process reads the
        vacant pathname as held by a live sibling and runs with ``mcpServers``
        withheld: the toolless-second-session symptom re-manufactured in-process.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        key = os.fspath(path)
        assert owner._seed_owner in sp._read_disk_seeds()[key]["holders"][sp._HOLDER_OWNERS]

        path.unlink()
        _teardown(owner)

        durable = sp._read_disk_seeds().get(key, {"holders": sp._empty_holders()})
        assert durable["holders"][sp._HOLDER_OWNERS] == {}
        assert sp.held_by_another(path, "a-later-session") is False
        # End to end: the next session in this process authors the vacant pathname
        # instead of declining under a claim nobody holds.
        later = _client(tmp_path, permission_mode="default")
        later._write_claude_local_settings()
        assert later._claude_settings_authored is True
        assert path.exists()

    def test_settle_releases_the_owner_holder_when_the_seed_was_replaced(
        self, tmp_path, monkeypatch
    ):
        """A seed the user replaced before teardown still hands back the owner holder.

        The replacement is the user's file: the settle transaction puts it back
        untouched and deletes nothing. The durable owner holder is Crew's own and
        must not outlive the session, or the pathname reads as held by a live
        sibling for the rest of the process once the user removes their file.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        key = os.fspath(path)
        foreign = '{"permissions": {"allow": ["Bash(ls)"]}}\n'

        path.write_text(foreign, encoding="utf-8")
        _teardown(owner)

        assert path.read_text(encoding="utf-8") == foreign
        durable = sp._read_disk_seeds().get(key, {"holders": sp._empty_holders()})
        assert durable["holders"][sp._HOLDER_OWNERS] == {}
        assert sp.held_by_another(path, "a-later-session") is False
        # The user's file is still left alone by the next session...
        later = _client(tmp_path, permission_mode="default")
        later._write_claude_local_settings()
        assert later._claude_settings_authored is False
        assert later._claude_settings_shared is False
        assert path.read_text(encoding="utf-8") == foreign
        # ...and once the user removes it, that session authors the vacant pathname
        # rather than declining under the departed owner's claim.
        path.unlink()
        later._write_claude_local_settings()
        assert later._claude_settings_authored is True
        assert path.exists()

    def test_settle_with_the_lock_broken_after_the_move_puts_the_seed_back(
        self, tmp_path, monkeypatch
    ):
        """A post-move registry failure restores the seed instead of stranding it.

        The settle transaction moves Crew's seed aside and then consults the
        sharer registry a second time. With the lock unopenable at that point, the
        consultation must read as "no sharer" so the revoke runs, fails on the
        same lock, and the moved inode is restored under its pathname -- a
        repairable orphan a later session recognizes, rather than a ``.crew-gc``
        file nothing names beside an empty pathname.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        before = path.read_bytes()
        payload = owner._claude_settings_written
        assert payload is not None
        # The move-aside vacates the pathname; the lock dies at that moment.
        _break_the_lock_when(monkeypatch, lambda: not path.exists())

        owner._settle_claude_settings_seed(
            path, owner._seed_owner, payload, owner._expected_settings_fingerprint()
        )

        assert path.exists()
        assert path.read_bytes() == before
        assert not list(path.parent.glob("*.crew-gc"))

    def test_reseed_replacement_drops_governance_when_forget_refuses(
        self, tmp_path, monkeypatch, caplog
    ):
        """A refused durable revoke at the re-seed drops authorship, keeps the retry.

        The file at the path is the user's by observation (the inode-pinned
        move-aside found a replacement), so ``_permission_surface_governed`` must
        answer False from this return on -- a retained ``_claude_settings_authored``
        delivered the ``mcpServers`` array under the user's own permission file.
        The durable owner holder that could not be handed back rides
        ``_claude_settings_claim_unrevoked`` instead, which teardown reads and
        governance does not.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        replacement = '{"permissions": {"allow": ["Bash(rm -rf *)"]}}\n'

        def _user_replaces_in_the_gap(_path: Path, _expectation) -> None:
            # The replacement lands between the ownership probe and the move-aside,
            # which is the window this return exists for; an earlier replacement
            # takes the replaced-after-create branch instead.
            path.write_text(replacement, encoding="utf-8")
            return None

        monkeypatch.setattr(
            AcpClient, "_claim_pathname_if_ours", staticmethod(_user_replaces_in_the_gap)
        )
        real_forget = sp.forget
        monkeypatch.setattr(sp, "forget", lambda _path, _owner: False)
        owner._session_mcp_cache = [{"name": "stale-crew-tools"}]
        owner._session_mcp_snapshot = acp_client.DerivedSpecSnapshot("old", "old")
        with caplog.at_level(logging.WARNING, logger=acp_client.__name__):
            owner._write_claude_local_settings()

        assert owner._claude_settings_authored is False
        assert owner._claude_settings_written is None
        assert owner._permission_surface_governed is False
        assert owner._claude_settings_claim_unrevoked is True
        assert owner._session_mcp_cache is None
        assert owner._session_mcp_snapshot is None
        assert "could not durably forget" in caplog.text
        assert "retaining authorship" not in caplog.text
        assert path.read_text(encoding="utf-8") == replacement

        # Teardown still retries the hand-back: with the sidecar writable again the
        # whole-record revoke lands, the flag clears, and the user's file is untouched.
        forgotten: list[str] = []

        def _forget(candidate: Path | str, owner_id: str) -> bool:
            forgotten.append(owner_id)
            return real_forget(candidate, owner_id)

        monkeypatch.setattr(sp, "forget", _forget)
        _teardown(owner)
        assert forgotten == [owner._seed_owner]
        assert owner._claude_settings_claim_unrevoked is False
        assert sp.recorded_durable(path) is None
        assert os.fspath(path) not in sp._LIVE
        assert path.read_text(encoding="utf-8") == replacement

    def test_foreign_replace_under_sharer_with_failed_release_is_not_governed(
        self, tmp_path, monkeypatch, caplog
    ):
        """The double-refusal exit of the foreign-replace branch withholds the array.

        Crew authored the file; a live sibling shares it; the user replaces it
        atomically. ``forget`` fails closed under the sharer's lease and
        ``release`` fails too (the sidecar cannot be written). The file is the
        user's by observation, so the owner must NOT stay governed: a retained
        ``_claude_settings_authored`` let ``_permission_surface_governed`` answer
        True and the ``mcpServers`` array was delivered under a permission file
        whose ``permissions.allow`` never reaches ``session/request_permission``.
        The un-revoked owner holder is carried on ``_claude_settings_claim_unrevoked``.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert sibling._claude_settings_shared is True
        assert sp.has_sharers(path) is True

        replacement = '{"permissions": {"allow": ["mcp__kirocrew-core__spawn_run"]}}\n'
        path.write_text(replacement, encoding="utf-8")
        forgets: list[str] = []
        releases: list[str] = []
        monkeypatch.setattr(sp, "forget", lambda _p, o: forgets.append(o) or False)
        monkeypatch.setattr(sp, "release", lambda _p, o: releases.append(o) or False)
        owner._session_mcp_cache = [{"name": "stale-crew-tools"}]
        owner._session_mcp_snapshot = acp_client.DerivedSpecSnapshot("old", "old")
        with caplog.at_level(logging.WARNING, logger=acp_client.__name__):
            owner._write_claude_local_settings()

        assert forgets == [owner._seed_owner]
        assert releases == [owner._seed_owner]
        assert owner._claude_settings_authored is False
        assert owner._claude_settings_written is None
        assert owner._permission_surface_governed is False
        assert owner._claude_settings_claim_unrevoked is True
        assert owner._session_mcp_cache is None
        assert owner._session_mcp_snapshot is None
        assert "retaining authorship" not in caplog.text
        assert path.read_text(encoding="utf-8") == replacement
        # The array itself, resolved the way the spawn path resolves it: the
        # claude mirror withholds both halves, pooled stubs included.
        stub = {"name": "kirocrew-core", "command": "/stub", "args": [], "env": [], "type": "stdio"}
        projection = acp_client.mirror_for(ACP_BACKEND_CLAUDE).session_projection(
            owner._agent,
            stub_server_names=("kirocrew-core",),
            stub_elements=[stub],
            permission_surface_owned=owner._permission_surface_governed,
        )
        assert projection.params == {"mcpServers": []}
        # The sibling's lease is untouched by the owner's failed hand-back.
        assert sibling._permission_surface_governed is True
        assert sp.has_sharers(path) is True

    def test_discard_retries_the_hand_back_for_an_unrevoked_claim(self, tmp_path, monkeypatch):
        """Teardown with only the retry flag set hands back the owner holder.

        No settle transaction: the file on disk is the user's, so nothing is
        moved or unlinked. Off the loop, ``forget`` is tried first (still refused
        under the live sharer) and ``release`` then drops only this owner's
        holder; the flag clears on that success and the sharer keeps its lease.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        replacement = '{"permissions": {"allow": ["Bash(ls)"]}}\n'
        path.write_text(replacement, encoding="utf-8")
        real_forget, real_release = sp.forget, sp.release
        monkeypatch.setattr(sp, "forget", lambda _p, _o: False)
        monkeypatch.setattr(sp, "release", lambda _p, _o: False)
        owner._write_claude_local_settings()
        assert owner._claude_settings_authored is False
        assert owner._claude_settings_claim_unrevoked is True
        assert sp._LIVE[os.fspath(path)] == owner._seed_owner

        forgets: list[str] = []
        releases: list[str] = []

        def _forget(candidate: Path | str, owner_id: str) -> bool:
            forgets.append(owner_id)
            return real_forget(candidate, owner_id)

        def _release(candidate: Path | str, owner_id: str) -> bool:
            releases.append(owner_id)
            return real_release(candidate, owner_id)

        monkeypatch.setattr(sp, "forget", _forget)
        monkeypatch.setattr(sp, "release", _release)

        def _no_settle(*_args, **_kwargs):
            raise AssertionError("a foreign file must not enter the move/unlink settle")

        monkeypatch.setattr(AcpClient, "_claim_pathname_if_ours", staticmethod(_no_settle))
        monkeypatch.setattr(AcpClient, "_settle_claude_settings_seed", _no_settle)
        _teardown(owner)

        assert forgets == [owner._seed_owner]
        assert releases == [owner._seed_owner]
        assert owner._claude_settings_claim_unrevoked is False
        assert path.read_text(encoding="utf-8") == replacement
        assert os.fspath(path) not in sp._LIVE
        assert sp.held_by_another(path, "a-later-session") is False
        # The sharer's lease and the record it validated both stand.
        assert sp.has_sharers(path) is True
        assert sp.recorded_durable(path) is not None

    def test_discard_total_refusal_leaves_the_unrevoked_claim_to_reset_state(
        self, tmp_path, monkeypatch, caplog
    ):
        """Both hand-backs refused at teardown: the flag stays for ``_reset_state``.

        The same shape as a refused reader-lease withdrawal: the discard leaves
        the instance flag set, and the synchronous reset then drops only the
        in-memory live slot so the process is not wedged, while the persisted
        holder is reclaimable once this process exits.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        replacement = '{"permissions": {"allow": ["Bash(ls)"]}}\n'
        path.write_text(replacement, encoding="utf-8")
        monkeypatch.setattr(sp, "forget", lambda _p, _o: False)
        monkeypatch.setattr(sp, "release", lambda _p, _o: False)
        owner._write_claude_local_settings()
        assert owner._claude_settings_claim_unrevoked is True

        with caplog.at_level(logging.WARNING, logger=acp_client.__name__):
            asyncio.run(owner._discard_claude_settings_seed())
        assert owner._claude_settings_claim_unrevoked is True
        assert sp._LIVE[os.fspath(path)] == owner._seed_owner
        assert "could not durably hand back" in caplog.text

        owner._reset_state()
        assert owner._claude_settings_claim_unrevoked is False
        assert os.fspath(path) not in sp._LIVE
        assert path.read_text(encoding="utf-8") == replacement

    def test_reset_state_drops_the_live_slot_after_a_failed_durable_release(
        self, tmp_path, monkeypatch, caplog
    ):
        """An authored teardown whose durable withdrawal fails still frees the slot.

        With the registry lock unopenable, the discard's ``forget`` and
        ``release`` are both refused and the seed returns to its pathname. Each
        withdrawal drops its in-memory half while the durable holder stands until
        the sidecar is writable again or this process exits. The reset remains an
        idempotent fallback for every teardown path.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        key = os.fspath(path)
        before = path.read_bytes()
        assert sp._LIVE[key] == owner._seed_owner

        _break_the_lock_when(monkeypatch, lambda: True)
        with caplog.at_level(logging.WARNING, logger=acp_client.__name__):
            asyncio.run(owner._discard_claude_settings_seed())
        assert "could not durably release Crew's claim" in caplog.text
        assert owner._claude_settings_authored is False
        assert owner._claude_settings_claim_unrevoked is False
        assert path.read_bytes() == before
        assert key not in sp._LIVE

        owner._reset_state()

        assert key not in sp._LIVE
        assert sp.held_by_another(path, "a-later-session") is False
        # The durable owner holder is the half the reset cannot reach.
        persisted = sp._read_disk_seeds()[key]["holders"][sp._HOLDER_OWNERS]
        assert owner._seed_owner in persisted

    def test_reset_does_not_drop_a_sibling_client_objects_lease(self, tmp_path, monkeypatch):
        """Positive control: the reset hands back only ITS OWN token's slot.

        Two clients in one process legitimately hold the same path under
        different owner tokens. A sibling's reset must not read a holder that
        carries this process's identity as withdrawn, or one session's teardown
        drops another's live lease and a third session may overwrite a file
        still in use. ``release_local`` compares the token, so the owner's slot
        and its persisted holder both survive.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        key = os.fspath(path)
        assert sp._LIVE[key] == owner._seed_owner

        sibling = _client(tmp_path, permission_mode="default")
        assert sibling._seed_owner != owner._seed_owner
        sibling._reset_state()

        assert sp._LIVE[key] == owner._seed_owner
        assert owner._claude_settings_is_still_ours() is True
        identity = sp._read_disk_seeds()[key]["holders"][sp._HOLDER_OWNERS][owner._seed_owner]
        assert sp._holder_is_live(identity) is True
        assert sp.held_by_another(path, sibling._seed_owner) is True

    def test_foreign_replace_clears_governance_when_the_hand_back_lands(
        self, tmp_path, monkeypatch
    ):
        """Positive control: the retry flag is set ONLY on the double-refusal exit.

        Without a sharer ``forget`` lands; under a sharer ``forget`` is refused
        and ``release`` lands. Both ordinary foreign-replace exits end with
        authorship dropped and no un-revoked claim carried.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        replacement = '{"permissions": {"allow": ["Bash(ls)"]}}\n'
        path.write_text(replacement, encoding="utf-8")
        owner._write_claude_local_settings()
        assert owner._claude_settings_authored is False
        assert owner._claude_settings_written is None
        assert owner._claude_settings_claim_unrevoked is False
        assert sp.recorded_durable(path) is None
        assert path.read_text(encoding="utf-8") == replacement

        second = _client(tmp_path, permission_mode="default")
        path.unlink()
        second._write_claude_local_settings()
        assert second._claude_settings_authored is True
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert sibling._claude_settings_shared is True
        path.write_text(replacement, encoding="utf-8")
        second._write_claude_local_settings()
        assert second._claude_settings_authored is False
        assert second._claude_settings_written is None
        assert second._claude_settings_claim_unrevoked is False
        assert sp.has_sharers(path) is True
        assert os.fspath(path) not in sp._LIVE

    def test_owner_teardown_with_a_live_sharer_leaves_the_seed(self, tmp_path, monkeypatch):
        """The file a sharer delivered tools against must outlive the owner.

        Unlinking it would free the pathname for a DIFFERENT permission file --
        another session's defaultMode, up to bypassPermissions -- under an MCP
        array already delivered. So the teardown leaves file and record: the
        recorded-orphan shape a kill -9 already produces, repaired later.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        before = path.read_text(encoding="utf-8")
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert sibling._claude_settings_shared is True

        _teardown(owner)

        assert path.read_text(encoding="utf-8") == before
        assert sp.has_sharers(path) is True
        # The record survives with the file, so it stays recognizable as Crew's.
        assert sp.recorded(path, "a-later-session") is not None
        # Only the owner's durable holder is handed back; the sharer's stays with
        # the record, so a later adoption still refuses while that reader lives.
        holders = sp._read_disk_seeds()[os.fspath(path)]["holders"]
        assert owner._seed_owner not in holders[sp._HOLDER_OWNERS]
        assert sibling._seed_owner in holders[sp._HOLDER_SHARERS]

    def test_adoption_is_refused_while_a_sharer_lives(self, tmp_path, monkeypatch):
        """No Crew session may rewrite bytes a sharer is running against."""
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        before = path.read_text(encoding="utf-8")
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        _teardown(owner)

        # Directly: the slot is not takeable while a sharer is registered...
        assert sp.claim(path, "a-newcomer", expect_digest=None) is False
        # ...and end-to-end: a differing-payload newcomer neither adopts nor
        # shares -- it falls to the leave-it-alone branch, the pre-share behavior.
        newcomer = _client(tmp_path, permission_mode="bypassPermissions")
        newcomer._write_claude_local_settings()
        assert newcomer._claude_settings_authored is False
        assert newcomer._claude_settings_shared is False
        assert path.read_text(encoding="utf-8") == before
        # An identical-payload newcomer still gets the shared surface.
        joiner = _client(tmp_path, permission_mode="default")
        joiner._write_claude_local_settings()
        assert joiner._claude_settings_shared is True

    def test_the_orphan_is_repairable_once_the_last_sharer_leaves(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        _teardown(owner)
        assert path.exists()

        _teardown(sibling)

        assert sp.has_sharers(path) is False
        # The ordinary orphan lifecycle resumes: the next session adopts, re-seeds
        # with ITS configuration, and its own teardown removes the file.
        successor = _client(tmp_path, permission_mode="acceptEdits")
        successor._write_claude_local_settings()
        assert successor._claude_settings_authored is True
        assert _seed(tmp_path)["permissions"]["defaultMode"] == "acceptEdits"
        _teardown(successor)
        assert not path.exists()

    def test_owner_settle_restores_the_file_when_a_sharer_races_the_move(
        self, tmp_path, monkeypatch
    ):
        """The settle's sharer probe is re-run AFTER the move-aside.

        The probe and the move are not one atomic step: a sharer can register and
        validate in between (its disk check read the file before the move, and
        registration precedes validation, so it is visible to the re-check).
        Without the barrier the teardown frees the pathname under a governed
        reader.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        before = path.read_text(encoding="utf-8")

        real = AcpClient._claim_pathname_if_ours

        def move_then_sharer_appears(p, expectation):
            aside = real(p, expectation)
            sp._SHARERS.setdefault(os.fspath(path), set()).add("late-reader")
            return aside

        monkeypatch.setattr(
            AcpClient, "_claim_pathname_if_ours", staticmethod(move_then_sharer_appears)
        )
        _teardown(owner)

        assert path.read_text(encoding="utf-8") == before
        # Record kept with the file, so it stays a recognizable Crew seed.
        assert sp.recorded(path, "a-later-session") is not None

    def test_an_adoption_rewrite_stands_down_for_a_late_sharer(self, tmp_path, monkeypatch):
        """A sharer registering between claim() and the adoption's write wins.

        claim() refused adoption while sharers existed, so any sharer present
        after the write arrived in that window and validated the OLD bytes. The
        adoption restores them (the durable record still names them) and stands
        down rather than leaving a governed reader on bytes absent from the path.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        first = _client(tmp_path, permission_mode="default")
        first._write_claude_local_settings()
        path = _settings(tmp_path)
        before = path.read_text(encoding="utf-8")
        _the_owning_process_died()

        real_write = acp_client.atomic_write

        def write_then_sharer_appears(*args, **kwargs):
            real_write(*args, **kwargs)
            sp._SHARERS.setdefault(os.fspath(path), set()).add("late-reader")

        monkeypatch.setattr(acp_client, "atomic_write", write_then_sharer_appears)
        adopter = _client(tmp_path, permission_mode="bypassPermissions")
        adopter._write_claude_local_settings()

        assert path.read_text(encoding="utf-8") == before
        assert adopter._claude_settings_authored is False
        assert adopter._claude_settings_shared is False
        # The sharer's protection holds: the path is still not adoptable.
        assert sp.claim(path, "a-newcomer", expect_digest=None) is False

    def test_a_reseed_whose_record_fails_restores_the_prior_bytes(self, tmp_path, monkeypatch):
        """A re-seed whose durable record fails puts the recorded bytes back.

        The record for the new bytes publishes only once its sidecar persist
        lands, so a failing persist leaves the PRIOR grant durable. The re-seed
        has already renamed the new bytes into place by then, so the moved-aside
        prior file is restored: the path holds exactly the bytes the durable
        record names and stays the owner's recognized, repairable seed instead
        of an unrecorded file nothing on the host can clean up.
        """
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        owner, path, before = self._owner_with_a_sharer_and_a_warm_cache(
            tmp_path, monkeypatch, with_sharer=False
        )
        writes = self._spy_on_settings_writes(monkeypatch)
        monkeypatch.setattr(sp, "atomic_write", _unwritable_sidecar)

        owner._write_claude_local_settings()

        assert writes == [path]  # the new bytes did land before the record failed
        assert path.read_text(encoding="utf-8") == before
        assert sp.recorded_durable(path) == _fp(before)
        assert owner._claude_settings_written == before
        assert owner._claude_settings_is_still_ours() is True

    def test_an_undurable_record_cannot_seed_a_sharer(self, tmp_path, monkeypatch):
        """share() sees a record only once its persist has landed on disk."""
        path = tmp_path / "settings.local.json"
        payload = '{"permissions": {"defaultMode": "default"}}\n'
        monkeypatch.setattr(sp, "atomic_write", _unwritable_sidecar)
        assert sp.record(path, payload, _OWNER) is False
        assert sp.share(path, payload, "reader") is False
        assert sp.has_sharers(path) is False

    def test_an_owner_reseed_cannot_change_permissions_under_a_sharer(self, tmp_path, monkeypatch):
        """The re-seed refreshes model keys; it must not move the permission half.

        A sharer was delivered its MCP array under the file's defaultMode and
        deny rules. An agent-spec edit mid-session re-renders those, and writing
        the loosened set under a live reader would widen a surface it validated
        stricter -- so a re-seed whose permissions block differs keeps the file
        the sharers are running against.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        before = path.read_text(encoding="utf-8")
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert sibling._claude_settings_shared is True

        owner._permission_mode = "acceptEdits"  # the permission half moved
        owner._write_claude_local_settings()

        assert path.read_text(encoding="utf-8") == before
        assert owner._claude_settings_is_still_ours() is True
        assert sp.has_sharers(path) is True

    def test_a_late_sharer_beats_a_permissions_changing_reseed(self, tmp_path, monkeypatch):
        """The permissions barrier is re-run AFTER the owner's write.

        A sharer registering between the pre-write probes and the atomic_write
        validated the stricter bytes; publishing loosened deny rules under it
        would take its whole session out from behind the surface it validated.
        The re-seed retracts: the prior bytes return, and the owner keeps
        owning them.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        before = path.read_text(encoding="utf-8")

        real_write = acp_client.atomic_write

        def write_then_sharer_appears(*args, **kwargs):
            real_write(*args, **kwargs)
            sp._SHARERS.setdefault(os.fspath(path), set()).add("late-reader")

        monkeypatch.setattr(acp_client, "atomic_write", write_then_sharer_appears)
        owner._permission_mode = "acceptEdits"  # the permission half moved
        owner._write_claude_local_settings()

        assert path.read_text(encoding="utf-8") == before
        assert owner._claude_settings_is_still_ours() is True
        assert owner._claude_settings_authored is True

    @staticmethod
    def _owner_with_a_sharer_and_a_warm_cache(
        tmp_path: Path, monkeypatch, *, with_sharer: bool
    ) -> tuple[AcpClient, Path, str]:
        """An owner seeded on a cold cache whose next render carries model keys.

        The owner authors without model keys; a byte-identical sibling shares
        the file when ``with_sharer`` is set; then the cache warms, so the
        owner's next payload gains ``availableModels`` and ``model`` with an
        UNCHANGED ``permissions`` block -- the exact shape of the post-capture
        re-seed. Returns the owner, the path and the bytes on disk before it.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})
        owner = _client(tmp_path, permission_mode="default", model="claude-opus-5")
        owner._write_claude_local_settings()  # cold cache: no model keys yet
        path = _settings(tmp_path)
        before = path.read_text(encoding="utf-8")
        assert "availableModels" not in before
        if with_sharer:
            sibling = _client(tmp_path, permission_mode="default", model="claude-opus-5")
            sibling._write_claude_local_settings()
            assert sibling._claude_settings_shared is True
            assert sp.has_sharers(path) is True

        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner._model = mr.resolve_wire_model_id(owner._model, "claude_code")
        rendered = json.loads(owner._render_claude_settings_payload())
        # The digest moves (model keys) while the permissions block does not.
        assert rendered["availableModels"] == _SERVED
        assert rendered["permissions"] == json.loads(before)["permissions"]
        return owner, path, before

    @staticmethod
    def _spy_on_settings_writes(monkeypatch) -> list[Path]:
        """Record the target of every ``atomic_write`` the client performs."""
        real_write = acp_client.atomic_write
        writes: list[Path] = []

        def write_and_record(*args, **kwargs):
            writes.append(Path(args[0]))
            return real_write(*args, **kwargs)

        monkeypatch.setattr(acp_client, "atomic_write", write_and_record)
        return writes

    def test_a_model_key_reseed_is_declined_before_the_write_under_a_sharer(
        self, tmp_path, monkeypatch
    ):
        """A digest-changing re-seed is declined BEFORE the write under a sharer.

        ``record`` refuses any digest change while a live sharer holds the prior
        record, and a digest cannot tell a model-key refresh from a permissions
        change. So the owner declines up front rather than writing bytes the
        durable commit is bound to refuse and then retracting them: the file the
        sharer validated is never touched, and the owner's recorded payload
        stays the bytes on disk.
        """
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        owner, path, before = self._owner_with_a_sharer_and_a_warm_cache(
            tmp_path, monkeypatch, with_sharer=True
        )
        writes = self._spy_on_settings_writes(monkeypatch)

        owner._write_claude_local_settings()

        assert writes == []
        assert path.read_text(encoding="utf-8") == before
        assert owner._claude_settings_written == before
        assert owner._claude_settings_is_still_ours() is True
        assert sp.has_sharers(path) is True

    def test_the_declined_reseed_log_does_not_promise_a_next_session_refresh(
        self, tmp_path, monkeypatch, caplog
    ):
        """The decline says when the keys land: once no sibling is reading the file.

        A later session's re-seed is refused identically while the same sharer
        stays registered, so "the next session" is a promise the code cannot
        keep.
        """
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        owner, path, _before = self._owner_with_a_sharer_and_a_warm_cache(
            tmp_path, monkeypatch, with_sharer=True
        )

        with caplog.at_level(logging.INFO, logger=acp_client.__name__):
            owner._write_claude_local_settings()

        declines = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.INFO
            and "keeping the file the sharer validated" in record.getMessage()
        ]
        assert len(declines) == 1, caplog.text
        assert "refresh on the next session" not in declines[0]
        assert "once no sibling is reading" in declines[0]

    def test_a_model_key_reseed_still_writes_with_no_sharer(self, tmp_path, monkeypatch):
        """The barrier fires only under a sharer: alone, the owner's re-seed lands."""
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        owner, path, before = self._owner_with_a_sharer_and_a_warm_cache(
            tmp_path, monkeypatch, with_sharer=False
        )
        writes = self._spy_on_settings_writes(monkeypatch)

        owner._write_claude_local_settings()

        assert writes == [path]
        after = path.read_text(encoding="utf-8")
        assert after != before
        assert json.loads(after)["availableModels"] == _SERVED
        assert owner._claude_settings_written == after
        assert sp.recorded_durable(path) == _fp(after)
        assert owner._claude_settings_is_still_ours() is True

    def test_a_settle_restore_never_clobbers_a_recreated_file(self, tmp_path, monkeypatch):
        """The sharer-race restore is no-clobber.

        The pathname is free from the move-aside until the restore, so a
        settings file the user recreates in that window is theirs: the restore
        must preserve it and keep the moved seed as litter, never overwrite it.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        users_file = json.dumps({"permissions": {"allow": ["Bash(ls)"]}}, indent=2)

        real = AcpClient._claim_pathname_if_ours

        def move_then_sharer_and_user_race_in(p, expectation):
            aside = real(p, expectation)
            sp._SHARERS.setdefault(os.fspath(path), set()).add("late-reader")
            path.write_text(users_file, encoding="utf-8")
            return aside

        monkeypatch.setattr(
            AcpClient, "_claim_pathname_if_ours", staticmethod(move_then_sharer_and_user_race_in)
        )
        _teardown(owner)

        assert path.read_text(encoding="utf-8") == users_file

    def test_a_swapped_symlink_is_moved_back_never_dereferenced(self, tmp_path):
        """A symlink at the aside name goes back as the link itself, not its target's bytes.

        The aside name sits in the attacker-influenceable work dir: a sibling
        can replace the moved-aside file with a symlink to a credential file
        in the restore window. The rename moves the directory entry and reads
        nothing through it, so no regular file carrying the target's bytes is
        ever created; the link lands at the path exactly as the sibling made it.
        """
        secret = tmp_path / "credentials"
        secret.write_text("AKIA-SECRET", encoding="utf-8")
        aside = tmp_path / "settings.local.json.abc123.crew-gc"
        aside.write_text('{"permissions": {}}', encoding="utf-8")
        moved = os.lstat(aside)
        moved_ident = (moved.st_dev, moved.st_ino)
        aside.unlink()
        aside.symlink_to(secret)
        path = tmp_path / "settings.local.json"

        assert AcpClient._restore_aside_without_clobber(aside, path, moved_ident) is True
        assert path.is_symlink()
        assert path.resolve() == secret.resolve()
        assert not aside.exists() and not aside.is_symlink()

    def test_a_swapped_hard_link_is_moved_back_never_copied(self, tmp_path):
        """A hard link at the aside name goes back as that same inode, not a copy.

        A hard link is not a symlink: a following open reads the target's
        bytes. The rename moves the entry without opening it, so the inode at
        the path is the sibling's own link and no second file holding the
        credential bytes is created in the workspace.
        """
        secret = tmp_path / "credentials"
        secret.write_text("AKIA-SECRET", encoding="utf-8")
        aside = tmp_path / "settings.local.json.abc123.crew-gc"
        aside.write_text('{"permissions": {}}', encoding="utf-8")
        moved = os.lstat(aside)
        moved_ident = (moved.st_dev, moved.st_ino)
        aside.unlink()
        os.link(secret, aside)
        path = tmp_path / "settings.local.json"

        assert AcpClient._restore_aside_without_clobber(aside, path, moved_ident) is True
        assert os.lstat(path).st_ino == os.lstat(secret).st_ino
        assert not aside.exists()

    def test_an_ordinary_restore_lands_the_aside_and_consumes_it(self, tmp_path):
        """An ordinary restore lands the aside's bytes and consumes the aside."""
        aside = tmp_path / "settings.local.json.abc123.crew-gc"
        prior = json.dumps({"permissions": {"defaultMode": "default"}}, indent=2)
        aside.write_text(prior, encoding="utf-8")
        path = tmp_path / "settings.local.json"

        moved = os.lstat(aside)
        moved_ident = (moved.st_dev, moved.st_ino)
        assert AcpClient._restore_aside_without_clobber(aside, path, moved_ident) is True
        assert path.read_text(encoding="utf-8") == prior
        assert not aside.exists()
        # The staging temp is consumed too -- no .crew-gc litter survives success.
        assert not list(tmp_path.glob("*.crew-gc"))

    def test_a_restore_preserves_a_replacement_at_the_published_aside(self, tmp_path, monkeypatch):
        """POSIX preserves a replacement; Windows atomically consumes the aside."""
        monkeypatch.setattr(acp_client.platform_compat, "RENAME_NOREPLACE_AVAILABLE", False)
        aside = tmp_path / "settings.local.json.abc123.crew-gc"
        prior = b'{"permissions": {"defaultMode": "default"}}'
        aside.write_bytes(prior)
        path = tmp_path / "settings.local.json"
        moved = os.lstat(aside)
        moved_ident = (moved.st_dev, moved.st_ino)
        real_put_back = acp_client.pinned_fs.put_back_no_clobber
        fired: list[int] = []

        def publish_then_replace(*args, **kwargs):
            fired.append(1)
            result = real_put_back(*args, **kwargs)
            if result is None:
                aside.unlink()
                aside.write_bytes(b"foreign")
            return result

        monkeypatch.setattr(acp_client.pinned_fs, "put_back_no_clobber", publish_then_replace)

        assert AcpClient._restore_aside_without_clobber(aside, path, moved_ident) is True
        assert path.read_bytes() == prior
        if acp_client.platform_compat.IS_WINDOWS:
            assert fired == []
            assert not aside.exists()
        else:
            assert fired == [1]
            assert aside.read_bytes() == b"foreign"

    def test_the_restore_never_clobbers_an_occupant_of_the_pathname(self, tmp_path):
        """An occupant of the pathname is kept and the moved entry stays at the aside."""
        aside = tmp_path / "settings.local.json.abc123.crew-gc"
        aside.write_text('{"permissions": {}}', encoding="utf-8")
        path = tmp_path / "settings.local.json"
        users_file = json.dumps({"permissions": {"allow": ["Bash(ls)"]}}, indent=2)
        path.write_text(users_file, encoding="utf-8")

        moved = os.lstat(aside)
        moved_ident = (moved.st_dev, moved.st_ino)
        assert AcpClient._restore_aside_without_clobber(aside, path, moved_ident) is False
        assert path.read_text(encoding="utf-8") == users_file
        assert aside.exists()

    def test_share_validation_waits_for_a_settle_transaction(self, tmp_path, monkeypatch):
        """A sharer cannot validate inside a teardown's move/restore window.

        The teardown's own move-aside manufactures a vacancy at the pathname;
        a user replacement racing into it is preserved by the no-clobber
        restore. A share that validated the ORIGINAL bytes in that window
        would become governed against a file it never verified. The settle
        lock forces the validation to land before the move or after the
        transaction settles.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        payload = owner._claude_settings_written
        assert payload is not None
        sibling = _client(tmp_path, permission_mode="default")

        done = threading.Event()
        result: dict[str, bool] = {}

        def try_share() -> None:
            result["shared"] = sibling._share_settings_seed_if_identical(path, payload)
            done.set()

        with sp.SETTLE_LOCK:  # a settle transaction is mid-flight
            worker = threading.Thread(target=try_share)
            worker.start()
            # flake-ok: a 300 ms absence window while this test holds SETTLE_LOCK; moved verbatim
            assert not done.wait(0.3)  # the validation is held out of the window
        assert done.wait(5)
        worker.join(5)
        # The settled state still holds Crew's bytes, so the share lands.
        assert result["shared"] is True
        assert sibling._claude_settings_shared is True

    def test_a_replacement_landing_in_the_settle_window_is_never_shared(
        self, tmp_path, monkeypatch
    ):
        """A user replacement that takes the teardown's vacancy is not governed.

        The teardown moves Crew's seed aside; a replacement arrives at the
        vacated name before the transaction finishes. Serialized behind the
        settle lock, a later share validates the SETTLED state -- the
        replacement's bytes -- and declines, instead of validating the
        original bytes in the window and going governed under the swap.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        payload = owner._claude_settings_written
        assert payload is not None
        expectation = owner._expected_settings_fingerprint()

        real_claim = AcpClient._claim_pathname_if_ours

        def claim_then_replacement_races_in(p, exp):
            claimed = real_claim(p, exp)
            # The vacancy the transaction manufactures: a user save lands NOW.
            Path(p).write_text('{"permissions": {"defaultMode": "bypassPermissions"}}')
            return claimed

        monkeypatch.setattr(
            AcpClient, "_claim_pathname_if_ours", staticmethod(claim_then_replacement_races_in)
        )
        owner._settle_claude_settings_seed(path, owner._seed_owner, payload, expectation)
        monkeypatch.setattr(AcpClient, "_claim_pathname_if_ours", staticmethod(real_claim))

        sibling = _client(tmp_path, permission_mode="default")
        assert sibling._share_settings_seed_if_identical(path, payload) is False
        assert sibling._claude_settings_shared is not True
        assert sp.has_sharers(path) is False
        # The replacement is preserved, exactly as the no-clobber contract says.
        assert "bypassPermissions" in path.read_text(encoding="utf-8")
