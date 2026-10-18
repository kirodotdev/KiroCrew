"""A slot's dismissed source links across hydration and rebind.

Every path that binds a slot to a transcript restores the slot's dismissed set or
defers its write, and a rebind to another transcript starts from an empty set.
Shared links, slots and request builders live in
``dashboard_source_link_unlink_helpers.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from dashboard_source_link_unlink_helpers import (
    PR_A,
    PR_B,
    _identity_key,
    _slot,
)

from kiro_crew.dashboard import channel_slots
from kiro_crew.dashboard.state import _ChatSlot


class TestAlternateHydratorsRestoreDismissals:
    """The dismissed-source-link tombstones must be
    restored on EVERY hydration path, not only the two persistence loaders.

    ``surface_channel_session`` (and the resume path's ``_hydrate_slot_from_history``)
    re-apply metadata by hand rather than routing through ``_rehydrate_slot_from_history``. Before
    the fix they skipped ``dismissed_source_links``, so a re-surfaced channel
    session showed a chip the user had unlinked, and the next save -- serializing
    an empty dismissed set -- erased the persisted tombstone for good.
    """

    def test_surface_channel_session_restores_the_dismissed_set(self, tmp_path):
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path)
        key = _identity_key(PR_A)
        session_key = "weixin:kirocrew-research:direct:u1"
        info = {
            "key": "weixin_kirocrew-research_direct_u1",
            "title": "t",
            "modified": 0.0,
        }
        # The transcript mentions PR_A, so absent a restore the chip would derive.
        messages = [
            {"role": "assistant", "content": PR_A, "ts": "2026-09-01T00:00:00+00:00"},
        ]
        meta = {"dismissed_source_links": [key]}

        slot = channel_slots.surface_channel_session(
            state, info, meta, messages, session_key=session_key
        )
        assert slot is not None
        # The tombstone was restored from meta ...
        assert key in slot._dismissed_source_links
        # ... so the derived chip stays suppressed rather than resurrected.
        assert not any(link["url"] == PR_A for link in slot._pr_source_links())


class TestRebindResetsDismissals:
    """A slot's dismissed set is scoped to the transcript it is bound to. When
    the binding changes (a cron/workflow rebind of a live slot), the set belongs
    to the old transcript and must not ride into the new one, or the slot's next
    save persists those tombstones onto the new transcript and suppresses
    unrelated links there.
    """

    def test_hydration_with_no_key_clears_a_stale_set(self):
        # Hydration is authoritative: loading a transcript that records NO
        # dismissals must clear a set left over from a previous binding of a
        # reused slot object, not leave it in place.
        from kiro_crew.dashboard.chat_persistence import _restore_dismissed_source_links

        slot = _slot()
        slot.dismiss_source_link(_identity_key(PR_A))
        assert slot._dismissed_source_links
        _restore_dismissed_source_links(slot, None)  # new transcript has no key
        assert slot._dismissed_source_links == set()

    def test_hydration_restores_the_new_transcripts_dismissals(self):
        from kiro_crew.dashboard.chat_persistence import _restore_dismissed_source_links

        slot = _slot()
        slot.dismiss_source_link(_identity_key(PR_A))
        _restore_dismissed_source_links(slot, [_identity_key(PR_B)])
        assert slot._dismissed_source_links == {_identity_key(PR_B)}  # replaced, not merged

    def test_cron_bind_restores_persisted_dismissals(self):
        # _bind_cron_slot hydrates a cron slot from its transcript. Message
        # hydration carries rows only and get_or_create_slot clears the set on
        # the binding change, so the cron transcript's persisted dismissals must
        # be restored from the OFF-LOOP-prefetched value passed in — otherwise
        # the slot's next full save serializes an empty set and erases them.
        from kiro_crew.dashboard import cron_inject

        slot = _slot()
        state = MagicMock()
        state.get_or_create_slot.return_value = slot
        slot.linked_session_key = ""  # unbound -> triggers hydration branch
        job = MagicMock(id="job42", agent_id="")
        with (
            patch.object(cron_inject, "hydrate_slot_from_history"),
            patch("kiro_crew.dashboard.chat_utils._sync_dashboard_slots"),
            patch.object(cron_inject, "_safe_job_name", return_value="job42"),
        ):
            cron_inject._bind_cron_slot(state, job, [], dismissed=[_identity_key(PR_A)])
        assert slot._dismissed_source_links == {_identity_key(PR_A)}

    def test_cron_bind_defers_dismissed_write_when_metadata_unreadable(self):
        # On an unreadable read (default sentinel) the slot still BINDS to the
        # canonical cron transcript (routing/continuity must not split), but is
        # marked _dismissed_hydrated=False so its full save carries the on-disk
        # dismissed line forward instead of erasing it with an empty set.
        from kiro_crew.dashboard import cron_inject

        slot = _slot()
        state = MagicMock()
        state.get_or_create_slot.return_value = slot
        slot.linked_session_key = ""
        job = MagicMock(id="job42", agent_id="")
        with (
            patch.object(cron_inject, "hydrate_slot_from_history") as hyd,
            patch("kiro_crew.dashboard.chat_utils._sync_dashboard_slots"),
            patch.object(cron_inject, "_safe_job_name", return_value="job42"),
        ):
            cron_inject._bind_cron_slot(state, job, [])  # no dismissed arg -> sentinel
        assert slot.linked_session_key == "cron:job42"  # BOUND (routing preserved)
        hyd.assert_called_once()  # hydration moved with the link
        assert slot._dismissed_hydrated is False  # dismissed WRITE deferred

    def test_workflow_fallback_clears_a_reused_slots_stale_dismissals(self):
        # inject_workflow_result falls back to a dedicated ``workflow-<run_id>``
        # slot when the originating chat is gone. A reused fallback slot object
        # can still hold a PRIOR run's dismissed set; on the link change it must
        # be cleared and marked unhydrated (mirroring the cron bind), or the
        # slot's next union-save folds those foreign tombstones into the newly
        # linked transcript and suppresses unrelated links there.
        from kiro_crew.dashboard import workflow_inject

        slot = _slot()
        slot.dismiss_source_link(_identity_key(PR_A))  # a prior run's tombstone
        assert slot._dismissed_source_links
        slot.linked_session_key = ""  # reused fallback, not yet linked
        state = MagicMock()
        state.get_slot.return_value = None  # originating chat is gone -> fallback
        state.get_or_create_slot.return_value = slot
        snapshot = {"session_key": "sess-xyz", "name": "demo"}
        with patch("kiro_crew.dashboard.chat_utils._sync_dashboard_slots"):
            workflow_inject.inject_workflow_result(state, "run77", snapshot)
        assert slot.linked_session_key == "sess-xyz"  # linked to the new transcript
        assert slot._dismissed_source_links == set()  # stale set cleared
        assert slot._dismissed_hydrated is False  # write deferred until a readable restore


# Every function in the dashboard package that binds a live slot to a transcript
# AND applies that transcript's metadata or rows by hand. The per-slot dismissed
# set is a MIRROR of the transcript's ``dismissed_source_links`` line, so each of
# these must either restore the mirror (``_restore_dismissed_source_links``) or
# defer its write (``_dismissed_hydrated = False``) -- a site that does neither
# shows the user a chip they unlinked until the slot next hydrates. The
# structural pin below DISCOVERS the sites (a new hand-rolled bind path is found,
# not listed) and this set only asserts the scan has not gone blind. The three
# restore readers -- the open-tab restore, the recent-sessions restore and the
# History resume -- apply metadata through ``slot_persistence.metadata_codec``
# instead of by hand, so they are pinned at that table (the pin's last assertion)
# and by the end-to-end restore test each gets in the class below.
_KNOWN_HYDRATION_SITES = frozenset(
    {
        ("channel_slots", "surface_channel_session"),
        ("cron_inject", "_bind_cron_slot"),
        ("cron", "api_cron_to_chat"),
    }
)


class TestEveryHydrationPathRestoresOrDefers:
    """Completeness pin for the per-slot dismissed mirror.

    ``dismissed_source_links`` is slot-owned metadata mirrored into
    ``_ChatSlot._dismissed_source_links``. Two different things can go wrong when
    a bind path forgets the mirror, and they are pinned separately here:

    * DISPLAY: the re-surfaced slot derives a chip the user unlinked. Fixed only
      by restoring on that path -- so every path that applies transcript
      metadata or rows is enumerated structurally and checked, and the three
      loaders the behavioural suite above did not already exercise directly
      (``_rehydrate_slot_from_history``, ``_apply_recent_session``, the resume
      endpoint) get an end-to-end restore test each.
    * ERASURE: the forgetful slot's next full save serialises its empty set over
      the transcript's real tombstones. This class is closed by the save paths
      themselves, which UNION memory with the on-disk line -- so a bind path that
      forgets to hydrate regresses to a stale chip, never to data loss. Pinned
      by ``test_a_bind_path_that_forgets_to_hydrate_cannot_erase_a_tombstone``.
    """

    @staticmethod
    def _seed(state, hkey: str, key: str, *urls: str) -> None:
        for url in urls:
            state.conversation_log.append(hkey, "assistant", url)
        state.conversation_log.update_metadata(hkey, {"dismissed_source_links": [key]})

    def test_rehydrate_slot_from_history_restores_the_dismissed_set(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history

        state = _make_state(tmp_path)
        key = _identity_key(PR_A)
        self._seed(state, "dashboard:rh1", key, PR_A, PR_B)

        slot = _rehydrate_slot_from_history(state, "rh1")
        assert slot is not None
        assert slot._dismissed_source_links == {key}
        assert slot._dismissed_hydrated is True
        urls = {link["url"] for link in slot._pr_source_links()}
        assert PR_A not in urls and PR_B in urls  # suppressed, sibling intact

    def test_apply_recent_session_restores_the_dismissed_set(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard.chat_persistence import _apply_recent_session

        state = _make_state(tmp_path)
        key = _identity_key(PR_A)
        hkey = "dashboard:recent1"
        self._seed(state, hkey, key, PR_A, PR_B)
        meta = state.conversation_log._read_metadata(hkey) or {}
        messages = state.conversation_log.read_messages(hkey)

        _apply_recent_session(
            state,
            hkey,
            "recent1",
            {},
            meta,
            messages,
            conv_log=state.conversation_log,
            kiro_model_map={},
            restore_cfg=None,
        )
        slot = state._slots["recent1"]
        assert slot._dismissed_source_links == {key}
        urls = {link["url"] for link in slot._pr_source_links()}
        assert PR_A not in urls and PR_B in urls

    @pytest.mark.asyncio
    async def test_resume_endpoint_restores_the_dismissed_set(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app, _make_state

        state = _make_state(tmp_path)
        key = _identity_key(PR_A)
        hkey = "dashboard:resume1"
        self._seed(state, hkey, key, PR_A, PR_B)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/resume1/resume", json={"key": hkey})
            assert resp.status == 200
        slot = state._slots["resume1"]
        assert slot._dismissed_source_links == {key}
        urls = {link["url"] for link in slot._pr_source_links()}
        assert PR_A not in urls and PR_B in urls

    def test_every_metadata_applying_bind_site_restores_dismissals(self):
        """Structural pin: discover every dashboard function that applies a
        transcript's metadata (proxy: it assigns ``slot.autocompact_pct`` from
        ``meta``) or hydrates a slot's rows (calls ``hydrate_slot_from_history``),
        and require each to call ``_restore_dismissed_source_links``. A new
        hand-rolled bind path that skips the mirror fails HERE, by name, instead
        of regressing to a resurrected chip.
        """
        import ast
        import pathlib

        import kiro_crew.dashboard as pkg

        root = pathlib.Path(pkg.__file__).parent
        found: dict[tuple[str, str], bool] = {}
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                applies_meta = False
                hydrates_rows = False
                restores = False
                for node in ast.walk(fn):
                    # A restore site ASSIGNS the transcript's value onto the slot:
                    # ``slot.autocompact_pct = _validate_autocompact_pct(meta[...])``.
                    # The save path only READS the attribute into the metadata
                    # line, and the ``/autocompact`` endpoint assigns a REQUEST
                    # value; neither sources from ``meta``, so neither matches.
                    if (
                        isinstance(node, ast.Assign)
                        and any(
                            isinstance(t, ast.Attribute)
                            and isinstance(t.value, ast.Name)
                            and t.value.id == "slot"
                            and t.attr == "autocompact_pct"
                            for t in node.targets
                        )
                        and any(
                            isinstance(n, ast.Name) and n.id == "meta" for n in ast.walk(node.value)
                        )
                    ):
                        applies_meta = True
                    if isinstance(node, ast.Call):
                        callee = node.func
                        name = (
                            callee.id
                            if isinstance(callee, ast.Name)
                            else callee.attr if isinstance(callee, ast.Attribute) else ""
                        )
                        if name == "hydrate_slot_from_history":
                            hydrates_rows = True
                        if name == "_restore_dismissed_source_links":
                            restores = True
                if applies_meta or hydrates_rows:
                    found[(path.stem, fn.name)] = restores

        assert _KNOWN_HYDRATION_SITES <= found.keys(), (
            "the bind-site scan went blind; expected at least "
            f"{sorted(_KNOWN_HYDRATION_SITES - found.keys())}"
        )
        missing = sorted(site for site, ok in found.items() if not ok)
        assert not missing, (
            "these slot-bind paths apply transcript metadata/rows without restoring "
            f"the dismissed source-link mirror: {missing}"
        )
        # The restore readers' metadata goes through the codec's field table, and
        # its dismissed row must be read by every purpose the table serves.
        from kiro_crew.dashboard.slot_persistence import metadata_codec

        row = next(r for r in metadata_codec.FIELDS if r.key == "dismissed_source_links")
        assert row.purposes == metadata_codec.PURPOSES, (
            "a restore reader no longer restores the dismissed source-link mirror: "
            f"{sorted(metadata_codec.PURPOSES - row.purposes)}"
        )

    @pytest.mark.asyncio
    async def test_a_bind_path_that_forgets_to_hydrate_cannot_erase_a_tombstone(
        self, tmp_path, monkeypatch
    ):
        """The erasure class is closed by the save, not by each bind path.

        A slot bound with the DEFAULT flags (``_dismissed_hydrated=True``, empty
        set, no transaction in flight) -- exactly what a bind path that forgot
        the mirror produces -- must leave the transcript's tombstones intact on
        BOTH full-save branches (durable rows / empty window), because each
        unions memory with the on-disk line. The regression such a path CAN
        cause is display-only: the forgetful slot derives the chip.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

        state = _make_state(tmp_path)
        key = _identity_key(PR_A)
        for hkey, with_rows in (("cron:forgot-rows", True), ("cron:forgot-empty", False)):
            state.conversation_log.update_metadata(hkey, {"dismissed_source_links": [key]})
            slot = _ChatSlot(hkey.replace(":", "-"))
            slot.linked_session_key = hkey  # bound, nothing else touched
            if with_rows:
                slot.append("assistant", PR_A, ts="t1")
            assert slot._dismissed_source_links == set()
            assert slot._dismissed_hydrated is True and slot._dismissed_txn_depth == 0
            # Display-only regression: the forgetful slot shows the chip ...
            if with_rows:
                assert any(link["url"] == PR_A for link in slot._pr_source_links())

            await save_slot_off_loop(state, slot, force=True)

            # ... but its save cannot shrink the durable set.
            meta = state.conversation_log._read_metadata(hkey) or {}
            assert meta.get("dismissed_source_links") == [key], hkey
