"""GitHub source-link chips are one object whatever the owner/repo letter case.

GitHub resolves host, owner and repository names case-insensitively, so two
mentions that differ only in that case are one pull request: one chip, counted
once, refreshed once, and dismissed together. GitLab project paths stay
case-sensitive, and an issue and a pull request with the same number stay two.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from dashboard_source_link_unlink_helpers import _identity_key, _request

from kiro_crew.dashboard.chat_handlers import api_chat_slot_source_link_unlink
from kiro_crew.dashboard.chat_persistence import _restore_dismissed_source_links
from kiro_crew.dashboard.source_providers import contract, hosts
from kiro_crew.dashboard.source_providers.contract import is_valid_source_identity_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

MIXED = "https://github.com/KiroDotDev/KiroCrew/pull/21991"
LOWER = "https://github.com/kirodotdev/kirocrew/pull/21991"
OWNER_ONLY = "https://github.com/KIRODOTDEV/kirocrew/pull/21991"
REPO_ONLY = "https://github.com/kirodotdev/KIROCREW/pull/21991"


def _slot(*messages: str) -> _ChatSlot:
    """A slot whose transcript carries *messages* oldest first."""
    slot = _ChatSlot("s1")
    for i, content in enumerate(messages):
        slot.append("assistant", content, ts=f"t{i}")
    return slot


def _urls(slot: _ChatSlot) -> list[str]:
    return [link["url"] for link in slot._pr_source_links()]


def _legacy_key(url: str) -> str:
    """The key an unlink recorded when owner/repo were kept as written."""
    from kiro_crew.dashboard.handlers.source_providers import parse_source_url

    ref = parse_source_url(url)
    return json.dumps(
        [ref.provider, ref.host, ref.owner, ref.repo, ref.number, ref.project, ref.kind, ""],
        separators=(",", ":"),
    )


class TestOneChipPerObject:
    @pytest.mark.parametrize("other", [OWNER_ONLY, REPO_ONLY, MIXED])
    def test_case_variants_collapse_to_one_chip(self, other):
        slot = _slot(LOWER, other)
        assert _urls(slot) == [other]
        assert slot.to_dict()["source_links_total"] == 1

    def test_the_chip_shows_the_newest_spelling(self):
        assert _urls(_slot(MIXED, LOWER)) == [LOWER]
        assert _urls(_slot(LOWER, MIXED)) == [MIXED]

    def test_variants_count_once_toward_the_three_chip_budget(self):
        others = [f"https://github.com/acme/widgets/pull/{n}" for n in (1, 2)]
        slot = _slot(MIXED, LOWER, *others)
        shown = [link["url"] for link in slot.to_dict()["source_links"]]
        assert len(shown) == 3 and set(others) <= set(shown)
        assert slot.to_dict()["source_links_total"] == 3

    def test_variants_are_refreshed_for_status_once(self):
        slot = _slot(MIXED, LOWER)
        assert DashboardState._slot_chip_urls(slot) == [LOWER]

    def test_an_issue_and_a_pull_request_with_one_number_stay_two(self):
        slot = _slot(MIXED, "https://github.com/kirodotdev/kirocrew/issues/21991")
        assert {link["kind"] for link in slot._pr_source_links()} == {"change", "issue"}

    def test_gitlab_paths_differing_in_case_stay_two(self):
        slot = _slot(
            "https://gitlab.com/Group/Project/-/merge_requests/5",
            "https://gitlab.com/group/project/-/merge_requests/5",
        )
        assert len(_urls(slot)) == 2

    def test_enterprise_host_and_owner_casing_collapse(self, monkeypatch):
        monkeypatch.setattr(hosts, "_allowed_github_hosts", lambda: frozenset({"ghe.acme.io"}))
        slot = _slot(
            "https://GHE.Acme.io/Team/Tool/pull/7",
            "https://ghe.acme.io/team/tool/pull/7",
        )
        assert _urls(slot) == ["https://ghe.acme.io/team/tool/pull/7"]


class TestDismissal:
    def test_dismissing_one_spelling_hides_every_spelling(self):
        slot = _slot(MIXED)
        (chip,) = slot._pr_source_links()
        assert slot.dismiss_source_link(chip["identity"]) is True
        slot.append("assistant", LOWER, ts="t9")
        assert _urls(slot) == []

    def test_a_tombstone_under_the_old_case_sensitive_key_still_hides_the_chip(self):
        legacy = _legacy_key(MIXED)
        assert is_valid_source_identity_key(legacy)
        assert legacy != _identity_key(MIXED)
        slot = _slot(MIXED, LOWER)
        _restore_dismissed_source_links(slot, [legacy])
        assert _urls(slot) == []

    def test_mentions_matches_a_folded_key_across_spellings(self):
        assert _slot(MIXED).mentions_source_identity(_identity_key(LOWER))

    @pytest.mark.asyncio
    async def test_unlink_with_an_old_case_sensitive_key_records_the_folded_one(self):
        slot = _slot(MIXED)
        req = _request("s1", _legacy_key(MIXED), {"s1": slot})
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        assert slot._dismissed_source_links == {_identity_key(LOWER)}
        slot.append("assistant", LOWER, ts="t9")
        assert _urls(slot) == []


class TestOldTombstoneRetry:
    @pytest.mark.asyncio
    async def test_retrying_an_unlink_already_on_disk_under_an_old_key_is_idempotent(self):
        legacy = _legacy_key(MIXED)
        slot = _slot(MIXED)
        _restore_dismissed_source_links(slot, [legacy])
        req = _request("s1", legacy, {"s1": slot})
        req.app["state"].conversation_log.get_metadata_status.return_value = (
            {"_type": "metadata", "created_at": "t", "dismissed_source_links": [legacy]},
            True,
        )
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        assert slot._dismissed_source_links == {legacy}
        req.app["state"].conversation_log.update_metadata_if.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_on_disk_match_stops_the_scan_of_a_long_line(self):
        legacy = _legacy_key(MIXED)
        slot = _slot(MIXED)
        _restore_dismissed_source_links(slot, [legacy])
        filler = [_identity_key(f"https://github.com/acme/w/pull/{n}") for n in range(5000)]
        req = _request("s1", legacy, {"s1": slot})
        req.app["state"].conversation_log.get_metadata_status.return_value = (
            {"_type": "metadata", "created_at": "t", "dismissed_source_links": [legacy, *filler]},
            True,
        )
        real = contract.canonical_source_identity_key
        calls = []

        def counting(key):
            calls.append(key)
            return real(key)

        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch.object(contract, "canonical_source_identity_key", counting),
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        assert not set(filler) & set(calls)

    @pytest.mark.asyncio
    async def test_a_rebound_slot_keeps_the_dismissal_its_new_transcript_holds_under_an_old_key(
        self,
    ):
        primary, sibling = _slot(MIXED), _slot(MIXED)
        key = _identity_key(MIXED)
        hkeys = {id(primary): "authorized", id(sibling): "authorized"}

        def _rebind_sibling_then_persist(*_a, **_k):
            hkeys[id(sibling)] = "rebound-target"
            return True

        def _meta_status(hkey):
            disk = [_legacy_key(MIXED)] if hkey == "rebound-target" else []
            return ({"_type": "metadata", "created_at": "t", "dismissed_source_links": disk}, True)

        slots = {"s1": primary, "s2": sibling}
        req = _request("s1", key, slots)
        state = req.app["state"]
        state.conversation_log.get_metadata_status.side_effect = _meta_status
        state.conversation_log.update_metadata_if.side_effect = _rebind_sibling_then_persist
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key",
                new=lambda s: hkeys[id(s)],
            ),
        ):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        assert key in sibling._dismissed_source_links


class TestNonAsciiNames:
    def test_a_lookalike_owner_is_not_merged_with_the_real_one(self):
        kelvin = "https://github.com/\u212airodotdev/kirocrew/pull/21991"
        assert len(_urls(_slot(LOWER, kelvin))) == 2

    def test_folding_never_lengthens_a_key(self):
        url = "https://github.com/" + "\u0130" * 75 + "/w/pull/1"
        key = _identity_key(url)
        assert is_valid_source_identity_key(key)
        assert key == _legacy_key(url)


class TestCanonicalKey:
    def test_github_owner_and_repo_fold(self):
        from kiro_crew.dashboard.source_providers.contract import canonical_source_identity_key

        assert canonical_source_identity_key(_legacy_key(MIXED)) == _identity_key(LOWER)

    def test_gitlab_and_malformed_keys_are_unchanged(self):
        from kiro_crew.dashboard.source_providers.contract import canonical_source_identity_key

        gitlab = _identity_key("https://gitlab.com/Group/Project/-/merge_requests/5")
        assert canonical_source_identity_key(gitlab) == gitlab
        assert canonical_source_identity_key('["Not","valid"]') == '["Not","valid"]'
