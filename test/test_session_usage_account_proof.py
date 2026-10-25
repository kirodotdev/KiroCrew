"""Each credit refresh publishes a reading only for an account whoami proves.

``_fetch_usage_bg`` resolves kiro-cli's identity at the top of every refresh,
never from a memo, and hands its profile ARN to ``fetch_usage_limits`` as the
credential anchor. These tests pin that anchor, the second whoami adjacent to the
API publish, the whoami pair that brackets the ``/usage`` scrape, and what a
refresh publishes when the account switches, lapses, reports no identity, is a
Builder ID proven by its email alone, or is an external identity provider sign-in
whose whoami names only its account type. test_session_usage.py pins whoami parsing
and the identity coupling rule.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from session_usage_helpers import (
    _CACHED_A,
    _IDENTITY_A,
    _IDENTITY_B,
    SAMPLE_USAGE,
    _api_result,
    _mock_proc,
    _reset_usage_globals,
)

import kiro_crew.dashboard.handlers.sessions as sessions_mod


class TestIdentityIsNotStale:
    """Identity must be re-resolved on every refresh, never memoized.

    A gateway-lifetime cache misattributed credits after an account switch:
    Builder ID A cached -> user signs in as Builder ID B -> the refresh accepts
    B's sole credential and, with no profile ARN on either side, the coupling
    check's single-credential branch passed the STALE A identity onto B's
    credits. whoami is credit-free, so it is simply fetched every refresh.
    """

    @pytest.mark.asyncio
    async def test_identity_refetched_each_refresh(self):
        _reset_usage_globals()
        ARN = "arn:aws:codewhisperer:us-east-1:1:profile/A"
        api_dict = {"credits_used": 1.0, "credits_plan": 10.0, "source": "api", "_profile_arn": ARN}
        calls = []
        current = {"n": 0}

        async def fake_whoami(_bin):
            # A different account per REFRESH (the session was switched between
            # them), stable within one: both whoami reads of a refresh agree.
            calls.append(1)
            return {"email": f"user{current['n']}@corp.com", "_profile_arn": ARN}

        for n in (1, 2):
            current["n"] = n
            sessions_mod._usage_cache_ts = 0.0
            with (
                patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
                patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
                patch.object(
                    sessions_mod.kiro_usage_api,
                    "fetch_usage_limits",
                    return_value=_api_result(dict(api_dict)),
                ),
                patch.object(sessions_mod, "_fetch_whoami", fake_whoami),
            ):
                await sessions_mod._fetch_usage_bg()
        # Two refreshes -> two whoami pairs (top + adjacent to the publish), none
        # memoized, and the LATEST identity wins. The same ARN throughout proves
        # each pair describes one account, so every refresh publishes.
        assert len(calls) == 4, "whoami must not be memoized across refreshes"
        assert sessions_mod._usage_cache["email"] == "user2@corp.com"
        _reset_usage_globals()

    def test_no_lifetime_identity_cache_exists(self):
        # Guard against the memoization being reintroduced.
        assert not hasattr(sessions_mod, "_identity_cache")


class TestCredentialSelectionIsAnchored:
    """``_fetch_usage_bg`` resolves kiro-cli's own identity FIRST and hands its
    profile ARN to ``fetch_usage_limits``, so credential selection cannot land on
    a profile the user has signed out of."""

    ARN = "arn:aws:codewhisperer:us-east-1:1:profile/A"

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture, moved verbatim; no test of the class calls monkeypatch.undo()
    def _reset(self, monkeypatch):
        _reset_usage_globals()
        yield
        _reset_usage_globals()

    @pytest.mark.asyncio
    async def test_whoami_arn_is_passed_as_the_anchor(self):
        api_dict = {
            "credits_used": 1.0,
            "credits_plan": 10.0,
            "source": "api",
            "_profile_arn": self.ARN,
        }
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(
                sessions_mod,
                "_fetch_whoami",
                AsyncMock(return_value={"email": "me@corp.com", "_profile_arn": self.ARN}),
            ),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result(api_dict),
            ) as fetch,
        ):
            await sessions_mod._fetch_usage_bg()
        assert fetch.call_args.kwargs.get("expected_arn") == self.ARN

    @pytest.mark.asyncio
    async def test_api_branch_re_resolves_whoami_adjacent_to_the_publish(self):
        # The top-of-refresh whoami anchors the credential the API uses, so the
        # API's own ARN matching THAT identity proves nothing about who is signed
        # in by the time the numbers arrive. A second whoami, adjacent to the
        # publish, is what proves it: whoami, api, whoami -- exactly two.
        events: list[str] = []

        async def whoami(_bin):
            events.append("whoami")
            return {"email": "me@corp.com", "_profile_arn": self.ARN}

        def api(**_k):
            events.append("api")
            return _api_result(
                {
                    "credits_used": 1.0,
                    "credits_plan": 10.0,
                    "source": "api",
                    "_profile_arn": self.ARN,
                }
            )

        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=whoami)),
            patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", api),
        ):
            await sessions_mod._fetch_usage_bg()
        assert events == ["whoami", "api", "whoami"], events
        assert sessions_mod._usage_cache["credits_plan"] == 10.0
        assert sessions_mod._usage_cache["email"] == "me@corp.com"

    @pytest.mark.asyncio
    async def test_api_reading_is_discarded_when_the_account_switched_before_the_publish(self):
        # whoami says A at the top; the API (anchored on A) returns A's plan; by
        # the time it arrives the user is on B. Nothing of A may be published:
        # not the balance, not the email -- not even for the moment the adjacent
        # whoami takes -- and nothing of A that the cache already held is kept.
        arn_b = "arn:aws:codewhisperer:us-east-1:2:profile/B"
        sessions_mod._usage_cache = dict(_CACHED_A)  # A's earlier reading is on screen
        seen_during_second_whoami: list[dict] = []
        snapshots = iter(
            [
                {**_IDENTITY_A, "_profile_arn": self.ARN},
                {**_IDENTITY_B, "_profile_arn": arn_b},
            ]
        )

        async def whoami(_bin):
            seen_during_second_whoami.append(dict(sessions_mod._usage_cache))
            return next(snapshots)

        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=whoami)),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result(
                    {
                        "credits_used": 1.0,
                        "credits_plan": 10.0,
                        "source": "api",
                        "_profile_arn": self.ARN,
                    }
                ),
            ),
            patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn,
        ):
            outcome = await sessions_mod._fetch_usage_bg()
        assert outcome is None
        assert sessions_mod._usage_cache == {"available": False}
        assert "a@corp.com" not in str(sessions_mod._usage_cache)
        # The check comes BEFORE any publish: while the adjacent whoami ran, the
        # cache still held what it held before this refresh, never the new numbers.
        assert seen_during_second_whoami[1] == _CACHED_A
        assert spawn.await_count == 0  # no scrape is attempted for the previous account either

    @pytest.mark.asyncio
    async def test_api_reading_for_the_same_account_is_published_with_the_adjacent_fields(self):
        # Control for the test above: whoami stays A, so A's reading is published
        # -- labelled from the ADJACENT snapshot, not the top one.
        snapshots = [
            {**_IDENTITY_A, "_profile_arn": self.ARN},
            {**_IDENTITY_A, "_profile_arn": self.ARN, "account_type": "IamIdentityCenter"},
        ]
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=snapshots)),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result(
                    {
                        "credits_used": 1.0,
                        "credits_plan": 10.0,
                        "source": "api",
                        "_profile_arn": self.ARN,
                    }
                ),
            ),
        ):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache["credits_plan"] == 10.0
        assert sessions_mod._usage_cache["email"] == "a@corp.com"
        assert sessions_mod._usage_cache["account_type"] == "IamIdentityCenter"
        assert "_profile_arn" not in sessions_mod._usage_cache

    @pytest.mark.asyncio
    async def test_two_identity_less_whoamis_fall_through_to_the_scrape(self):
        # The sign-out half of a profile switch: whoami prints no identity, twice,
        # while the outgoing account's token is still on disk -- so the API,
        # anchored on nothing, returned THAT account's plan. Absence of a switch
        # is not evidence of an account: the API numbers are never published,
        # and the refresh falls through to kiro-cli's own /usage panel -- whose
        # reading is held to the same proof, so with whoami still silent on both
        # sides of the scrape the refresh ends with no reading at all.
        api_plan = {"credits_used": 777.0, "credits_plan": 9000.0, "source": "api"}
        published: list[dict] = []
        real_publish = sessions_mod._publish_usage

        def recording_publish(payload):
            published.append(dict(payload))
            real_publish(payload)

        spawn = AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode()))
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value={})),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result(dict(api_plan)),
            ) as fetch,
            patch.object(sessions_mod, "_publish_usage", recording_publish),
            patch("asyncio.create_subprocess_exec", spawn),
        ):
            await sessions_mod._fetch_usage_bg()
        assert fetch.call_args.kwargs.get("expected_arn") is None
        # The scrape WAS attempted (the fallback, not a dead end)...
        assert spawn.await_count == 1
        assert any("/usage" in call.args for call in spawn.call_args_list)
        # ...but its reading is unproven too: nothing is published, from either
        # source, at any moment.
        assert sessions_mod._usage_cache == {"available": False}
        assert published == [], published

    @pytest.mark.asyncio
    async def test_two_identity_less_whoamis_parked_surface_no_api_numbers(self):
        # Same unproven pair, scrape parked: the API reading must not ride the
        # parked marker either -- the frontend reads `credits_plan` before it
        # reads `available`, so a partial payload carrying it would render.
        # flake-ok: pre-existing real-clock read parking the scrape an hour ahead of the product's own monotonic clock, moved verbatim; tracked for a de-flake pass
        sessions_mod._usage_scrape_backoff_until = time.monotonic() + 3600
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value={})),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result({"credits_used": 777.0, "credits_plan": 9000.0}),
            ),
            patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn,
        ):
            outcome = await sessions_mod._fetch_usage_bg()
        assert outcome == sessions_mod._SKIPPED_SCRAPE_PARKED
        assert spawn.await_count == 0
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_an_identity_that_appeared_only_at_the_publish_is_a_switch(self):
        # Nothing at the top (the API anchored on nothing), B adjacent to the
        # publish: whoever the API read, it was not proven to be B. Discard,
        # keep nothing, and do not spend a scrape on the previous account.
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(
                sessions_mod, "_fetch_whoami", AsyncMock(side_effect=[{}, dict(_IDENTITY_B)])
            ),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result({"credits_used": 1.0, "credits_plan": 10.0}),
            ),
            patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn,
        ):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}
        assert spawn.await_count == 0

    # ---- the /usage scrape is bracketed too -------------------------------

    def _scrape_bracket(self, whoami_side_effect, stdout=SAMPLE_USAGE.encode(), prior=None):
        """Run one refresh whose API yields no plan, recording every publish and
        the order of whoami / api / spawn events."""
        events: list[str] = []
        published: list[dict] = []
        real_publish = sessions_mod._publish_usage
        snapshots = iter(whoami_side_effect)

        async def whoami(_bin):
            events.append("whoami")
            return next(snapshots)

        def api(**_k):
            events.append("api")
            return _api_result(None)

        async def spawn(*_a, **_k):
            events.append("spawn")
            return _mock_proc(stdout)

        def recording_publish(payload):
            published.append(dict(payload))
            real_publish(payload)

        if prior is not None:
            sessions_mod._usage_cache = dict(prior)
        return events, published, whoami, api, spawn, recording_publish

    @pytest.mark.asyncio
    async def test_the_scrape_is_bracketed_by_a_whoami_before_and_after(self):
        # whoami, api, whoami, spawn, whoami: one snapshot immediately before the
        # child is spawned and one immediately after it returns. The reading is
        # labelled from the AFTER snapshot (the one proven to have been signed in
        # throughout the read), so a field only that snapshot carries is what
        # lands on the reading.
        events, published, whoami, api, spawn, pub = self._scrape_bracket(
            [
                dict(_IDENTITY_A),
                dict(_IDENTITY_A),
                {**_IDENTITY_A, "account_type": "IamIdentityCenter"},
            ]
        )
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=whoami)),
            patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", api),
            patch.object(sessions_mod, "_publish_usage", pub),
            patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=spawn)),
        ):
            await sessions_mod._fetch_usage_bg()
        assert events == ["whoami", "api", "whoami", "spawn", "whoami"], events
        assert len(published) == 1
        assert published[0]["credits_plan"] == 10000.0
        assert published[0]["email"] == "a@corp.com"
        assert published[0]["account_type"] == "IamIdentityCenter"

    @pytest.mark.asyncio
    async def test_a_switch_between_the_scrape_and_its_after_whoami_publishes_nothing(self):
        # A before the child ran, B once it returned: whoever the child read, the
        # reading is unpublishable -- and A's prior reading is not kept either.
        events, published, whoami, api, spawn, pub = self._scrape_bracket(
            [dict(_IDENTITY_A), dict(_IDENTITY_A), dict(_IDENTITY_B)], prior=_CACHED_A
        )
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=whoami)),
            patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", api),
            patch.object(sessions_mod, "_publish_usage", pub),
            patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=spawn)),
        ):
            await sessions_mod._fetch_usage_bg()
        assert events == ["whoami", "api", "whoami", "spawn", "whoami"], events
        assert published == [], published
        assert sessions_mod._usage_cache == {"available": False}
        assert "a@corp.com" not in str(sessions_mod._usage_cache)
        assert "b@corp.com" not in str(sessions_mod._usage_cache)

    @pytest.mark.asyncio
    async def test_a_switch_before_the_scrape_is_caught_by_the_pre_spawn_whoami(self):
        # A at the top, B on both sides of the child: the child read B and the
        # bracket proves B, so B's reading publishes -- labelled B, never A.
        events, published, whoami, api, spawn, pub = self._scrape_bracket(
            [dict(_IDENTITY_A), dict(_IDENTITY_B), dict(_IDENTITY_B)], prior=_CACHED_A
        )
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=whoami)),
            patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", api),
            patch.object(sessions_mod, "_publish_usage", pub),
            patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=spawn)),
        ):
            await sessions_mod._fetch_usage_bg()
        assert len(published) == 1
        assert published[0]["email"] == "b@corp.com"
        assert sessions_mod._usage_cache["email"] == "b@corp.com"
        assert "a@corp.com" not in str(sessions_mod._usage_cache)

    @pytest.mark.asyncio
    async def test_two_empty_whoamis_around_the_scrape_publish_nothing(self):
        # A switch between two empty snapshots is invisible, so a scrape read
        # between them is unpublishable; there is no source left to fall back
        # to, so this refresh ends with no reading.
        events, published, whoami, api, spawn, pub = self._scrape_bracket(
            [{}, {}, {}], prior=_CACHED_A
        )
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=whoami)),
            patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", api),
            patch.object(sessions_mod, "_publish_usage", pub),
            patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=spawn)),
        ):
            await sessions_mod._fetch_usage_bg()
        assert events == ["whoami", "api", "whoami", "spawn", "whoami"], events
        assert published == [], published
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_a_top_whoami_that_failed_does_not_veto_a_proven_scrape(self):
        # The top whoami timed out (empty); the bracket around the child proves
        # A on both sides. The scrape is judged by ITS bracket, not the top.
        events, published, whoami, api, spawn, pub = self._scrape_bracket(
            [{}, dict(_IDENTITY_A), dict(_IDENTITY_A)]
        )
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=whoami)),
            patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", api),
            patch.object(sessions_mod, "_publish_usage", pub),
            patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=spawn)),
        ):
            await sessions_mod._fetch_usage_bg()
        assert len(published) == 1
        assert published[0]["email"] == "a@corp.com"

    @pytest.mark.asyncio
    async def test_a_parked_refresh_drops_the_api_partials_when_the_account_is_unproven(self):
        # The API's plan name came from a credential anchored on the top whoami;
        # it rides the parked marker only when the parked path's own whoami
        # proves that account is still signed in.
        # flake-ok: pre-existing real-clock read parking the scrape an hour ahead of the product's own monotonic clock, moved verbatim; tracked for a de-flake pass
        sessions_mod._usage_scrape_backoff_until = time.monotonic() + 3600
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(
                sessions_mod,
                "_fetch_whoami",
                AsyncMock(side_effect=[dict(_IDENTITY_A), dict(_IDENTITY_B)]),
            ),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result({"plan": "KIRO POWER", "resets": "2026-09-01"}),
            ),
            patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn,
        ):
            outcome = await sessions_mod._fetch_usage_bg()
        assert outcome == sessions_mod._SKIPPED_SCRAPE_PARKED
        assert spawn.await_count == 0
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_a_builder_id_pair_that_agrees_on_its_email_alone_publishes(self):
        # Builder ID reports neither an ARN nor a start_url: the email is the
        # whole evidence, and an unchanged one is agreement, not a refusal.
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(
                sessions_mod,
                "_fetch_whoami",
                AsyncMock(return_value={"email": "solo@b.com", "account_type": "BuilderId"}),
            ),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result({"credits_used": 3.0, "credits_plan": 50.0}),
            ),
        ):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache["credits_plan"] == 50.0
        # No ARN on either side: the numbers publish, the identity is not coupled.
        assert "email" not in sessions_mod._usage_cache

    @pytest.mark.asyncio
    async def test_the_same_email_in_a_different_org_is_a_switch(self):
        # Same human email, different Identity Center instance: the start_url
        # differs, so this is two accounts, exactly as _same_identity rules.
        snapshots = [dict(_IDENTITY_A), {**_IDENTITY_A, "start_url": _IDENTITY_B["start_url"]}]
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=snapshots)),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result({"credits_used": 1.0, "credits_plan": 10.0}),
            ),
        ):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_a_switch_seen_by_email_alone_is_still_a_switch(self):
        # Identity Center shape without ARNs: email + start_url flip -> refused.
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(
                sessions_mod,
                "_fetch_whoami",
                AsyncMock(side_effect=[dict(_IDENTITY_A), dict(_IDENTITY_B)]),
            ),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result({"credits_used": 1.0, "credits_plan": 10.0}),
            ),
        ):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_an_identity_that_vanished_before_the_publish_is_a_switch(self):
        # A at the top, nothing at all adjacent (the session lapsed mid-refresh):
        # evidence on one side only is a switch, not agreement -- discard, keep
        # nothing, no scrape for the account that is gone.
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(
                sessions_mod,
                "_fetch_whoami",
                AsyncMock(side_effect=[{**_IDENTITY_A, "_profile_arn": self.ARN}, {}]),
            ),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result(
                    {"credits_used": 1.0, "credits_plan": 10.0, "_profile_arn": self.ARN}
                ),
            ),
            patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn,
        ):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}
        assert spawn.await_count == 0

    @pytest.mark.asyncio
    async def test_text_branch_re_resolves_whoami_after_the_scrape(self):
        # The text branch merges the identity WITHOUT an ARN check, on the grounds
        # that the scrape and whoami are both kiro-cli's own output. That holds
        # only if they are read adjacently: up to ~2 minutes separates the
        # top-of-refresh whoami from the scrape (whoami 30s + API 30s + scrape
        # 60s), and a profile switch inside that window would pair the OLD email
        # with the NEW account's credits. So identity must be re-resolved AFTER
        # the scrape, and the fresh one must win.
        whoami = AsyncMock(
            side_effect=[
                {"email": "old@corp.com"},  # top of refresh (the anchor attempt)
                {"email": "new@corp.com"},  # immediately before the scrape
                {"email": "new@corp.com"},  # immediately after it (what must be shown)
            ]
        )
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(sessions_mod, "_fetch_whoami", whoami),
            patch.object(
                sessions_mod.kiro_usage_api, "fetch_usage_limits", return_value=_api_result(None)
            ),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode())),
            ),
        ):
            await sessions_mod._fetch_usage_bg()
        assert whoami.await_count == 3, "the scrape was not bracketed by whoami before and after"
        assert sessions_mod._usage_cache["source"] == "text"
        assert (
            sessions_mod._usage_cache["email"] == "new@corp.com"
        ), "cached the pre-scrape identity beside post-scrape credits"

    @pytest.mark.asyncio
    async def test_unresolvable_identity_asks_the_api_but_cannot_publish_it(self):
        # _fetch_whoami returns {} for ANY failure, including its 30s timeout. That
        # yields no ARN -- the API is still asked (with no ARN it anchors on
        # PROVENANCE, kiro-cli's own auth store), but with no identity on either
        # side of the refresh nothing proves WHOSE token that store held, so the
        # answer is not published; the /usage scrape is tried and, unproven for
        # the same reason, publishes nothing either.
        api_dict = {"credits_used": 1.0, "credits_plan": 10.0, "source": "api"}
        spawn = AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode()))
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value={})),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result(api_dict),
            ) as fetch,
            patch("asyncio.create_subprocess_exec", spawn),
        ):
            await sessions_mod._fetch_usage_bg()
        fetch.assert_called_once()
        assert fetch.call_args.kwargs.get("expected_arn") is None
        assert spawn.await_count == 1
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_arnless_identity_still_uses_the_api(self):
        # Builder ID: whoami resolves but carries no profile ARN. Same route, and
        # this is the case that would otherwise bill the smallest quotas every
        # refresh, forever.
        api_dict = {"credits_used": 3.0, "credits_plan": 50.0, "source": "api"}
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(
                sessions_mod,
                "_fetch_whoami",
                AsyncMock(return_value={"email": "solo@b.com", "account_type": "BuilderId"}),
            ),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result(api_dict),
            ) as fetch,
        ):
            await sessions_mod._fetch_usage_bg()
        fetch.assert_called_once()
        assert fetch.call_args.kwargs.get("expected_arn") is None
        assert sessions_mod._usage_cache["source"] == "api"

    @pytest.mark.asyncio
    async def test_arnless_identity_does_not_spawn_the_billed_scrape(self):
        # The harm being prevented, asserted directly: no `kiro-cli chat ... /usage`
        # subprocess for a profile-less account when the API succeeds. The account
        # is still PROVEN across the refresh by its email, which Builder ID reports.
        api_dict = {"credits_used": 3.0, "credits_plan": 50.0, "source": "api"}
        spawn = AsyncMock()
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(
                sessions_mod,
                "_fetch_whoami",
                AsyncMock(return_value={"email": "solo@b.com", "account_type": "BuilderId"}),
            ),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result(api_dict),
            ),
            patch("asyncio.create_subprocess_exec", spawn),
        ):
            await sessions_mod._fetch_usage_bg()
        for call in spawn.call_args_list:
            assert "/usage" not in call.args, f"billed scrape spawned: {call.args}"

    @pytest.mark.asyncio
    async def test_private_coupling_key_never_reaches_the_cache(self):
        api_dict = {
            "credits_used": 1.0,
            "credits_plan": 10.0,
            "source": "api",
            "_profile_arn": self.ARN,
        }
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(
                sessions_mod, "_fetch_whoami", AsyncMock(return_value={"_profile_arn": self.ARN})
            ),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result(api_dict),
            ),
        ):
            await sessions_mod._fetch_usage_bg()
        assert "_profile_arn" not in sessions_mod._usage_cache


# -- An account whose whoami carries only an account type --
#
# An external identity provider sign-in answers whoami with
# ``{"accountType":"ExternalIdP","email":""}``: no email, no ``startUrl`` and no
# profile ARN. A type names a provider, not a user, so the account is shown and its
# balance reported unavailable; no API or scrape reading is published. The API call
# runs only so a refused credential keeps the ``signin_required`` remedy; when it
# finds no credential at all, the ``/usage`` scrape runs once and its outcome alone
# decides between that remedy and the no-balance state.

#: The whoami stdout of an external identity provider sign-in.
EXTERNAL_IDP_WHOAMI = b'{"accountType":"ExternalIdP","email":""}\n'

#: The ``auth_kv`` key kiro-cli stores the external identity provider token under.
EXTERNAL_IDP_KEY = "kirocli:external-idp:token"

EXTERNAL_IDP_SAMPLE_USAGE = (
    "Estimated Usage\n"
    "Credits used: 2480.1\n"
    "You have covered in plan (2480.1 of 10000) credits, "
    "resets on 2026-11-01 | KIRO POWER\n"
)

ACCOUNT_ONLY = {"account_type": "ExternalIdP", "available": False, "reason": "account_unproven"}
SIGNIN_REQUIRED = {"account_type": "ExternalIdP", "available": False, "reason": "signin_required"}


class _ResetUsageGlobals:
    """Base for the account-type-only classes: fresh refresh state around each test."""

    @pytest.fixture(autouse=True)
    def _reset_usage_state(self):
        _reset_usage_globals()
        yield
        _reset_usage_globals()


def _proc(stdout: bytes) -> MagicMock:
    proc = _mock_proc(stdout)
    proc.returncode = 0
    return proc


def _spawn_by_argv(
    whoami_outputs: list[bytes],
    usage_stdout: bytes = EXTERNAL_IDP_SAMPLE_USAGE.encode(),
    on_usage=None,
):
    """Fake ``create_subprocess_exec``: whoami calls answer from ``whoami_outputs``
    in order (the last one repeats), the ``/usage`` scrape answers ``usage_stdout``
    after running ``on_usage`` (a sign-in change while the child runs).

    Goes through the real ``_fetch_whoami_or_none`` parse, so the test exercises
    real whoami bytes rather than a hand-built identity dict."""
    calls: list[str] = []
    remaining = list(whoami_outputs)

    async def spawn(*argv, **_kwargs):
        if "whoami" in argv:
            calls.append("whoami")
            out = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            return _proc(out)
        calls.append("usage")
        if on_usage is not None:
            on_usage()
        return _proc(usage_stdout)

    return spawn, calls


def _patches(spawn, api_result):
    return (
        patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
        patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
        patch.object(
            sessions_mod.kiro_usage_api,
            "fetch_usage_limits",
            MagicMock(return_value=api_result),
        ),
        patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=spawn)),
    )


def _publish_spy():
    """Record every payload ``_publish_usage`` receives, still publishing it.

    The final cache alone cannot show that a number was never published: a
    reading published and then overwritten by the marker a few awaits later
    would still have been served to any reader in between."""
    return patch.object(sessions_mod, "_publish_usage", wraps=sessions_mod._publish_usage)


def _assert_only_markers_published(published) -> None:
    allowed = set(ACCOUNT_ONLY)
    assert published.call_args_list, "nothing was published"
    for call in published.call_args_list:
        assert set(call.args[0]) <= allowed, call.args[0]


class TestWhoamiShapeFromTheIssue(_ResetUsageGlobals):
    @pytest.mark.asyncio
    async def test_parses_to_an_account_type_and_nothing_else(self):
        spawn, _ = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        with (
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=spawn)),
        ):
            identity = await sessions_mod._fetch_whoami_or_none("/bin/kiro")
        assert identity == {"account_type": "ExternalIdP"}


class TestAccountTypeOnly(_ResetUsageGlobals):
    def test_the_issue_shape_is_type_only(self):
        assert sessions_mod._account_type_only({"account_type": "ExternalIdP"})

    @pytest.mark.parametrize("spelling", ["ExternalIdp", "external_idp", "EXTERNALIDP"])
    def test_the_token_store_spellings_count(self, spelling):
        assert sessions_mod._account_type_only({"account_type": spelling})

    @pytest.mark.parametrize(
        "identity",
        [
            {},
            {"account_type": ""},
            # Only ExternalIdP: another type reporting only itself takes the
            # ordinary path and keeps that path's remedy.
            {"account_type": "BuilderId"},
            {"account_type": "IamIdentityCenter"},
            {"account_type": "SocialGitHub"},
            {"account_type": "SomeFutureProvider"},
            {"account_type": "ExternalIdP", "email": "a@example.com"},
            {"account_type": "IamIdentityCenter", "email": "a@example.com"},
            {"account_type": "IamIdentityCenter", "start_url": "https://x.example/start"},
            {"account_type": "IamIdentityCenter", "_profile_arn": "arn:aws:x"},
        ],
    )
    def test_anything_naming_a_user_or_no_type_is_not(self, identity):
        assert not sessions_mod._account_type_only(identity)


class TestExternalIdpRefresh(_ResetUsageGlobals):
    @pytest.mark.asyncio
    async def test_the_account_is_shown_with_no_balance_and_nothing_is_published(self):
        # The usage API answers 403 and the scrape would parse a 10000-credit
        # plan. Nothing ties either to the user: the API runs only for its
        # auth_state, the scrape never runs, and the account is shown with no
        # balance.
        spawn, calls = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        p = _patches(spawn, _api_result(None))
        with p[0], p[1], p[2] as api, p[3]:
            await sessions_mod._fetch_usage_bg()
        assert calls == ["whoami"], calls
        api.assert_called_once_with(expected_arn=None)
        assert sessions_mod._usage_cache == ACCOUNT_ONLY

    @pytest.mark.asyncio
    async def test_a_reading_the_api_returns_is_never_published(self):
        spawn, calls = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        plan = {"credits_used": 10.0, "credits_plan": 10000.0, "source": "api"}
        p = _patches(spawn, _api_result(plan))
        with p[0], p[1], p[2], p[3], _publish_spy() as published:
            await sessions_mod._fetch_usage_bg()
        assert "usage" not in calls
        _assert_only_markers_published(published)
        assert sessions_mod._usage_cache == ACCOUNT_ONLY

    @pytest.mark.asyncio
    async def test_an_earlier_reading_is_not_kept(self):
        # The earlier reading may be another user's: it is not kept on screen
        # under an account the refresh cannot tie it to.
        sessions_mod._usage_cache = {
            "credits_plan": 10000.0,
            "credits_used": 10.0,
            "account_type": "ExternalIdP",
        }
        spawn, _ = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        p = _patches(spawn, _api_result(None))
        with p[0], p[1], p[2], p[3]:
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == ACCOUNT_ONLY

    @pytest.mark.asyncio
    async def test_the_state_holds_while_the_scrape_is_parked(self):
        # A parked or failing scrape cannot turn the state back into a bare
        # failure, because the scrape is never consulted for this shape.
        sessions_mod._usage_scrape_backoff_until = 9e18
        spawn, calls = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        p = _patches(spawn, _api_result(None))
        with p[0], p[1], p[2], p[3]:
            await sessions_mod._fetch_usage_bg()
        assert "usage" not in calls
        assert sessions_mod._usage_cache == ACCOUNT_ONLY

    @pytest.mark.asyncio
    async def test_the_scrape_back_off_is_untouched(self):
        sessions_mod._usage_scrape_failures = 2
        spawn, _ = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        p = _patches(spawn, _api_result(None))
        with p[0], p[1], p[2], p[3]:
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_scrape_failures == 2

    @pytest.mark.asyncio
    async def test_a_refused_credential_keeps_the_signin_remedy_without_a_scrape(self):
        # A 401 from GetUsageLimits is the server refusing the credential: a
        # sign-in problem, not "this sign-in never shows a balance". No scrape
        # is needed to corroborate it.
        spawn, calls = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        api = sessions_mod.kiro_usage_api
        p = _patches(spawn, api.UsageResult(None, api.AUTH_REJECTED))
        with p[0], p[1], p[2] as fetch, p[3]:
            await sessions_mod._fetch_usage_bg()
        fetch.assert_called_once_with(expected_arn=None)
        assert calls == ["whoami"], calls
        assert sessions_mod._usage_cache == SIGNIN_REQUIRED

    @pytest.mark.asyncio
    async def test_no_credential_with_a_working_scrape_stays_account_unproven(self):
        # The reader saw no unexpired credential, but kiro-cli still answers
        # /usage (it renews its own store, or reads one this reader does not
        # enumerate): the sign-in works, so "sign in again" would be wrong. The
        # scrape's 10000-credit plan is never published.
        spawn, calls = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        api = sessions_mod.kiro_usage_api
        p = _patches(spawn, api.UsageResult(None, api.AUTH_NO_CREDENTIAL))
        with p[0], p[1], p[2], p[3], _publish_spy() as published:
            await sessions_mod._fetch_usage_bg()
        assert calls == ["whoami", "usage"], calls
        assert sessions_mod._usage_cache == ACCOUNT_ONLY
        assert sessions_mod._usage_scrape_failures == 0
        _assert_only_markers_published(published)

    @pytest.mark.asyncio
    async def test_no_credential_with_a_failing_scrape_asks_for_a_sign_in(self):
        # No credential AND kiro-cli cannot answer /usage either: that pair is
        # the evidence a lapsed sign-in leaves, so the remedy is named. The
        # failed attempt feeds the back-off like any other scrape.
        spawn, calls = _spawn_by_argv([EXTERNAL_IDP_WHOAMI], usage_stdout=b"")
        api = sessions_mod.kiro_usage_api
        p = _patches(spawn, api.UsageResult(None, api.AUTH_NO_CREDENTIAL))
        with p[0], p[1], p[2], p[3], _publish_spy() as published:
            await sessions_mod._fetch_usage_bg()
        assert calls == ["whoami", "usage"], calls
        assert sessions_mod._usage_cache == SIGNIN_REQUIRED
        assert sessions_mod._usage_scrape_failures == 1
        _assert_only_markers_published(published)

    @pytest.mark.asyncio
    async def test_a_corroborating_scrape_that_parks_reports_the_park(self):
        # The third consecutive miss parks the scrape. The refresh tells its
        # caller so, as the ordinary path does, instead of reporting nothing.
        sessions_mod._usage_scrape_failures = sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD - 1
        spawn, calls = _spawn_by_argv([EXTERNAL_IDP_WHOAMI], usage_stdout=b"")
        api = sessions_mod.kiro_usage_api
        p = _patches(spawn, api.UsageResult(None, api.AUTH_NO_CREDENTIAL))
        with p[0], p[1], p[2], p[3]:
            result = await sessions_mod._fetch_usage_bg()
        assert calls == ["whoami", "usage"], calls
        assert result == sessions_mod._SKIPPED_SCRAPE_PARKED
        assert sessions_mod._usage_cache == SIGNIN_REQUIRED

    @pytest.mark.asyncio
    async def test_no_credential_while_the_scrape_is_parked_asks_for_a_sign_in(self):
        # A parked scrape has already failed repeatedly; that is the evidence,
        # as on the ordinary path's parked marker, and nothing is spawned.
        sessions_mod._usage_scrape_backoff_until = 9e18
        spawn, calls = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        api = sessions_mod.kiro_usage_api
        p = _patches(spawn, api.UsageResult(None, api.AUTH_NO_CREDENTIAL))
        with p[0], p[1], p[2], p[3]:
            await sessions_mod._fetch_usage_bg()
        assert calls == ["whoami"], calls
        assert sessions_mod._usage_cache == SIGNIN_REQUIRED

    @pytest.mark.asyncio
    async def test_a_non_auth_failure_stays_account_unproven(self):
        # A 403 is not auth-class, so the sign-in-type state stands and no
        # "sign in again" is claimed.
        spawn, _ = _spawn_by_argv([EXTERNAL_IDP_WHOAMI], usage_stdout=b"")
        api = sessions_mod.kiro_usage_api
        p = _patches(spawn, api.UsageResult(None, api.AUTH_OTHER))
        with p[0], p[1], p[2], p[3]:
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == ACCOUNT_ONLY

    @pytest.mark.asyncio
    async def test_an_email_on_both_sides_still_publishes(self):
        # A per-user field proven on both sides publishes the scrape.
        whoami = b'{"accountType":"IamIdentityCenter","email":"a@example.com"}\n'
        spawn, calls = _spawn_by_argv([whoami])
        p = _patches(spawn, _api_result(None))
        with p[0], p[1], p[2], p[3]:
            await sessions_mod._fetch_usage_bg()
        assert "usage" in calls
        cache = sessions_mod._usage_cache
        assert cache.get("credits_plan") == 10000.0, cache
        assert cache.get("email") == "a@example.com"


class TestASwitchWithinOneRegistration(_ResetUsageGlobals):
    """User A signed in when the refresh starts, user B of the SAME IdP
    registration by the time any read would land. kiro-cli's real auth store is
    used, so the registration-level claims it keeps (``client_id``, ``scopes``)
    are identical for A and B and only per-user fields differ."""

    @staticmethod
    def _write_store(home, user: str) -> None:
        from kiro_crew import identity_stores

        db = identity_stores.selected_store(sys.platform, home)
        db.parent.mkdir(parents=True, exist_ok=True)
        blob = {
            "access_token": f"token-{user}",
            "refresh_token": f"refresh-{user}",
            "expires_at": "2026-10-08T01:00:00Z",
            "client_id": "org-registration",
            "scopes": ["openid", "offline_access"],
            "issuer_url": "https://idp.example",
            "token_endpoint": "https://idp.example/token",
            "sub": f"user-{user}",
        }
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE IF NOT EXISTS auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("DELETE FROM auth_kv")
        con.execute("INSERT INTO auth_kv VALUES (?, ?)", (EXTERNAL_IDP_KEY, json.dumps(blob)))
        con.commit()
        con.close()

    @pytest.mark.asyncio
    async def test_a_switch_from_a_to_b_does_not_publish_as_b(self, tmp_path, monkeypatch):
        from kiro_crew import kiro_prerequisite

        for var in ("XDG_DATA_HOME", "LOCALAPPDATA", "APPDATA"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(kiro_prerequisite.Path, "home", lambda: tmp_path)
        self._write_store(tmp_path, "a")
        a_plan = {"credits_used": 10.0, "credits_plan": 10000.0, "source": "api"}
        spawn, calls = _spawn_by_argv(
            [EXTERNAL_IDP_WHOAMI], on_usage=lambda: self._write_store(tmp_path, "b")
        )
        p = _patches(spawn, _api_result(dict(a_plan)))
        with (
            p[0],
            p[1],
            p[2],
            p[3],
            patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True),
        ):
            # The switch lands while the refresh runs; a second refresh then
            # reads B's store.
            await sessions_mod._fetch_usage_bg()
            self._write_store(tmp_path, "b")
            await sessions_mod._fetch_usage_bg()
        cache = sessions_mod._usage_cache
        assert cache.get("credits_plan") is None, "A's balance was published while B is signed in"
        assert cache == ACCOUNT_ONLY


class TestOtherTypesKeepTheirRemedy(_ResetUsageGlobals):
    """A Builder ID or Identity Center whoami that prints only its type (an
    Identity Center sign-in with no readable start URL is an expected shape)
    is not "this sign-in type": its sign-in has lapsed, and the ordinary path
    names that remedy. It must not be marked ``account_unproven``."""

    @pytest.mark.parametrize("account_type", ["BuilderId", "IamIdentityCenter"])
    @pytest.mark.asyncio
    async def test_type_only_non_external_idp_keeps_signin_remedy(self, account_type):
        whoami = ('{"accountType":"%s","email":""}\n' % account_type).encode()

        async def spawn(*argv, **_kwargs):
            if "whoami" in argv:
                return _proc(whoami)
            failed = _proc(b"")
            failed.wait = AsyncMock(return_value=1)
            failed.returncode = 1
            return failed

        api = sessions_mod.kiro_usage_api
        p = _patches(spawn, api.UsageResult(None, api.AUTH_NO_CREDENTIAL))
        with p[0], p[1], p[2], p[3]:
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("available") is False
        assert (
            sessions_mod._usage_cache.get("reason") == "signin_required"
        ), sessions_mod._usage_cache


class TestAFailureInTheBranch(_ResetUsageGlobals):
    """A timeout or error inside the account-type-only branch leaves that
    branch's no-balance marker, not the "no reading yet" one, whose wording
    promises a reading this sign-in never gets."""

    @pytest.mark.asyncio
    async def test_an_error_in_the_api_call_leaves_account_unproven(self):
        spawn, _ = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        p = _patches(spawn, _api_result(None))
        with p[0], p[1], p[2] as fetch, p[3]:
            fetch.side_effect = RuntimeError("executor shut down")
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == ACCOUNT_ONLY

    @pytest.mark.asyncio
    async def test_a_timeout_leaves_account_unproven(self):
        # The API call hangs past the refresh deadline (a wedged TLS handshake):
        # it is held on an event released only after the refresh returned, so
        # the deadline is what ends it. The deadline expires only once the hang
        # has been entered, so setup taking any amount of time cannot fire it
        # early and land the refresh on the ordinary failure path instead.
        spawn, _ = _spawn_by_argv([EXTERNAL_IDP_WHOAMI])
        p = _patches(spawn, _api_result(None))
        release = threading.Event()
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        real_wait_for = asyncio.wait_for
        deadline = object()

        def _hang(**_kwargs):
            loop.call_soon_threadsafe(entered.set)
            release.wait(30)
            return _api_result(None)

        async def _deadline_after_entry(aw, timeout):
            if timeout is not deadline:
                return await real_wait_for(aw, timeout)
            task = asyncio.ensure_future(aw)
            entry = asyncio.ensure_future(entered.wait())
            done, _ = await asyncio.wait(
                {task, entry}, timeout=10, return_when=asyncio.FIRST_COMPLETED
            )
            entry.cancel()
            assert done, "neither the refresh nor the API call finished within 10 s"
            assert not task.done(), "the refresh ended before the API call hung"
            # The real wait_for, already out of time: it cancels the refresh
            # and raises TimeoutError exactly as the expired deadline does.
            return await real_wait_for(task, 0)

        try:
            with (
                p[0],
                p[1],
                p[2] as fetch,
                p[3],
                patch.object(sessions_mod, "_USAGE_FETCH_DEADLINE_SECS", deadline),
                patch.object(sessions_mod.asyncio, "wait_for", _deadline_after_entry),
            ):
                fetch.side_effect = _hang
                await sessions_mod._fetch_usage_bg()
        finally:
            release.set()
        assert entered.is_set()
        assert sessions_mod._usage_cache == ACCOUNT_ONLY

    @pytest.mark.asyncio
    async def test_a_switch_seen_after_the_error_is_judged_as_usual(self):
        # The whoami adjacent to the failure names a user: the branch's marker
        # does not describe that sign-in, so the ordinary failure path runs.
        named = b'{"accountType":"IamIdentityCenter","email":"a@example.com"}\n'
        spawn, _ = _spawn_by_argv([EXTERNAL_IDP_WHOAMI, named])
        p = _patches(spawn, _api_result(None))
        with p[0], p[1], p[2] as fetch, p[3]:
            fetch.side_effect = RuntimeError("executor shut down")
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("reason") != "account_unproven"
        assert sessions_mod._usage_cache.get("available") is False
