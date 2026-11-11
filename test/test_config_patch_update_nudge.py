"""PATCH /api/config/kirocrew for ``dashboard.update_nudge``, the update popup's
snooze/skip record."""

from __future__ import annotations

import json
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_config_patch import _make_app, _patch, tmp_config  # noqa: F401


class TestUpdateNudgeKeys:
    """The proactive update popup's per-version snooze/skip persistence.

    One atomic dict write, since the fields form one verdict. The strict spec
    (every required key present, ``skipped_versions`` optional, no extras) keeps it
    from being a generic JSON passthrough.
    """

    _REC = {"version": "99.5.0", "snoozed_until": 1756000000.0, "skipped": True}
    # The whole record the route stores for _REC, as a change card reads it.
    _FULL = {**_REC, "skipped_versions": ["99.5.0"]}

    @pytest.mark.asyncio
    async def test_full_record_round_trips_atomically(self, tmp_config) -> None:  # noqa: F811
        # Stored with the gateway's own clock beside it, which the unattended-apply
        # hold counts from (update_deferral.user_deferred_update).
        before = time.time()
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "dashboard.update_nudge", self._REC)).status == 200
        after = time.time()
        stored = json.loads(tmp_config.read_text(encoding="utf-8"))["dashboard"]["update_nudge"]
        assert stored == {**self._FULL, "stored_at": stored["stored_at"]}
        assert before <= stored["stored_at"] <= after

    @pytest.mark.asyncio
    async def test_the_stamp_is_neither_refused_nor_read_back(
        self, tmp_config  # noqa: F811
    ) -> None:
        # A client that writes back the record it read sends the stamp too; the
        # route keeps its own. A change card reads the whole record it writes, so
        # its post-apply check holds and its Undo writes back a valid record.
        from kiro_crew.change_card_catalog import KIND_SETTING_CHANGE
        from kiro_crew.dashboard import change_cards as cards

        async with TestClient(TestServer(_make_app())) as c:
            rec = {**self._FULL, "stored_at": 1.0}
            assert (await _patch(c, "dashboard.update_nudge", rec)).status == 200
        stored = json.loads(tmp_config.read_text(encoding="utf-8"))["dashboard"]["update_nudge"]
        assert stored["stored_at"] > 1.0
        params = {"path": "dashboard.update_nudge", "value": self._FULL}
        after = cards._sync_read_state(KIND_SETTING_CHANGE, params, [])
        assert after["value"] == self._FULL
        assert cards.after_shows_the_write(KIND_SETTING_CHANGE, params, after)

    @pytest.mark.asyncio
    async def test_non_object_rejected(self, tmp_config) -> None:  # noqa: F811
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "dashboard.update_nudge", "0.5.0")).status == 400

    @pytest.mark.asyncio
    async def test_missing_key_rejected(self, tmp_config) -> None:  # noqa: F811
        async with TestClient(TestServer(_make_app())) as c:
            rec = {"version": "0.5.0", "skipped": True}
            assert (await _patch(c, "dashboard.update_nudge", rec)).status == 400

    @pytest.mark.asyncio
    async def test_unknown_key_rejected(self, tmp_config) -> None:  # noqa: F811
        async with TestClient(TestServer(_make_app())) as c:
            rec = {**self._REC, "extra": 1}
            assert (await _patch(c, "dashboard.update_nudge", rec)).status == 400

    @pytest.mark.asyncio
    async def test_version_overlong_rejected(self, tmp_config) -> None:  # noqa: F811
        async with TestClient(TestServer(_make_app())) as c:
            rec = {**self._REC, "version": "x" * 129}
            assert (await _patch(c, "dashboard.update_nudge", rec)).status == 400

    @pytest.mark.asyncio
    async def test_negative_snooze_rejected(self, tmp_config) -> None:  # noqa: F811
        async with TestClient(TestServer(_make_app())) as c:
            rec = {**self._REC, "snoozed_until": -1.0}
            assert (await _patch(c, "dashboard.update_nudge", rec)).status == 400

    @pytest.mark.asyncio
    async def test_bool_snooze_rejected(self, tmp_config) -> None:  # noqa: F811
        # bool is an int subclass; a bare float() coercion would store 1.0.
        async with TestClient(TestServer(_make_app())) as c:
            rec = {**self._REC, "snoozed_until": True}
            assert (await _patch(c, "dashboard.update_nudge", rec)).status == 400

    @pytest.mark.asyncio
    async def test_skipped_wrong_type_rejected(self, tmp_config) -> None:  # noqa: F811
        async with TestClient(TestServer(_make_app())) as c:
            rec = {**self._REC, "skipped": "yes"}
            assert (await _patch(c, "dashboard.update_nudge", rec)).status == 400

    @pytest.mark.asyncio
    async def test_skipped_versions_round_trips(self, tmp_config) -> None:  # noqa: F811
        # The per-version skip list a snooze carries forward.
        rec = {**self._REC, "skipped_versions": ["99.4.0", "99.5.0"]}
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "dashboard.update_nudge", rec)).status == 200
        stored = json.loads(tmp_config.read_text(encoding="utf-8"))["dashboard"]["update_nudge"]
        assert stored["skipped_versions"] == ["99.4.0", "99.5.0"]

    @pytest.mark.asyncio
    async def test_a_stale_snooze_keeps_the_saved_skips(self, tmp_config) -> None:  # noqa: F811
        # One dashboard skips 0.6.0; another, still holding the empty record,
        # snoozes 0.6.1. The stored record keeps both skips.
        skip = {"version": "99.6.0", "snoozed_until": 0.0, "skipped": True}
        snooze = {"version": "99.6.1", "snoozed_until": 1756000000.0, "skipped": False}
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "dashboard.update_nudge", skip)).status == 200
            assert (await _patch(c, "dashboard.update_nudge", snooze)).status == 200
        stored = json.loads(tmp_config.read_text(encoding="utf-8"))["dashboard"]["update_nudge"]
        assert stored["version"] == "99.6.1"
        assert stored["skipped_versions"] == ["99.6.0"]

    @pytest.mark.asyncio
    async def test_a_skip_past_the_cap_is_refused_without_a_write(
        self, tmp_config  # noqa: F811
    ) -> None:
        from kiro_crew.update_deferral import MAX_SKIPPED_VERSIONS

        full = {
            "version": "99.0.0",
            "snoozed_until": 0.0,
            "skipped": True,
            "skipped_versions": [f"99.0.{i}" for i in range(MAX_SKIPPED_VERSIONS)],
        }
        skip = {"version": "99.1.0", "snoozed_until": 0.0, "skipped": True}
        snooze = {"version": "99.1.0", "snoozed_until": 1756000000.0, "skipped": False}
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "dashboard.update_nudge", full)).status == 200
            refused = await _patch(c, "dashboard.update_nudge", skip)
            assert refused.status == 409
            assert (await refused.json())["code"] == "skip_list_full"
            stored = json.loads(tmp_config.read_text(encoding="utf-8"))["dashboard"]
            assert stored["update_nudge"]["skipped_versions"] == full["skipped_versions"]
            # A stale snooze on the full list keeps every skip.
            assert (await _patch(c, "dashboard.update_nudge", snooze)).status == 200
        stored = json.loads(tmp_config.read_text(encoding="utf-8"))["dashboard"]["update_nudge"]
        assert stored["skipped_versions"] == full["skipped_versions"]

    @pytest.mark.asyncio
    async def test_a_whole_record_replaces_the_list(self, tmp_config) -> None:  # noqa: F811
        # A change card's Undo restores the record it replaced, list included,
        # so the skip the card added is really gone.
        skip = {"version": "99.6.0", "snoozed_until": 0.0, "skipped": True}
        before = {**skip, "skipped": False, "skipped_versions": []}
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "dashboard.update_nudge", skip)).status == 200
            assert (await _patch(c, "dashboard.update_nudge", before)).status == 200
        stored = json.loads(tmp_config.read_text(encoding="utf-8"))["dashboard"]["update_nudge"]
        assert stored["skipped"] is False and stored["skipped_versions"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "skips",
        ["0.5.0", [1], ["x" * 129], [f"v{i}" for i in range(101)]],
    )
    async def test_bad_skipped_versions_rejected(self, tmp_config, skips) -> None:  # noqa: F811
        async with TestClient(TestServer(_make_app())) as c:
            rec = {**self._REC, "skipped_versions": skips}
            assert (await _patch(c, "dashboard.update_nudge", rec)).status == 400
