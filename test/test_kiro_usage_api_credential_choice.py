"""Tests for how kiro_crew.dashboard.handlers.kiro_usage_api chooses the credential
it sends: candidate ordering, the IdC userId ownership check, the profile cache cap,
the internal-symlink and sensitive-path reads, ARN selection and the external IdP
token type.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from kiro_usage_api_helpers import _cand, _resp

import kiro_crew.dashboard.handlers.kiro_usage_api as api


class TestCandidateOrdering:
    """Candidates rank kiro-cli's own store first, then by expiry (freshest
    first), never by path order."""

    def test_freshest_expiry_ranks_first(self, tmp_path):
        # A stale-but-unexpired JSON credential sits in the highest-priority PATH
        # slot; the SQLite store holds a newer one. Deciding on path order here
        # would let a signed-out profile win.
        now = datetime.now(timezone.utc)
        soon = (now + timedelta(minutes=20)).isoformat()
        later = (now + timedelta(hours=8)).isoformat()
        stale = json.dumps({"accessToken": "older-json", "expiresAt": soon}).encode()

        db = tmp_path / "data.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        con.execute(
            "INSERT INTO auth_kv VALUES (?, ?)",
            (
                "kirocli:odic:token",
                json.dumps({"access_token": "newer-sqlite", "expires_at": later}),
            ),
        )
        con.commit()
        con.close()

        def fake_read(read_id):
            return stale if read_id.endswith("cli") else None

        with (
            patch("kiro_crew.hooks.safe_read_file_internal", side_effect=fake_read),
            patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True),
            patch.object(api, "_CLI_SQLITE_DBS", (db,)),
            patch.object(api, "_OTHER_SQLITE_DBS", ()),
        ):
            cands = api._candidate_tokens()
        assert [c.token for c in cands] == ["newer-sqlite", "older-json"]
        # Provenance rides along: the sqlite one is kiro-cli's own store,
        # the JSON SSO cache is not.
        assert [c.from_cli_store for c in cands] == [True, False]

    def test_equal_expiry_keeps_path_precedence(self):
        # Stable sort: with nothing to choose between them, the original source
        # order stands rather than an arbitrary one.
        same = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

        def fake_read(read_id):
            name = "cli-tok" if read_id.endswith("cli") else "ide-tok"
            return json.dumps({"accessToken": name, "expiresAt": same}).encode()

        with (
            patch("kiro_crew.hooks.safe_read_file_internal", side_effect=fake_read),
            patch.object(api, "_CLI_SQLITE_DBS", ()),
            patch.object(api, "_OTHER_SQLITE_DBS", ()),
        ):
            assert [c.token for c in api._candidate_tokens()] == ["cli-tok", "ide-tok"]

    def test_duplicate_token_keeps_latest_expiry_and_appears_once(self):
        # The same token in two stores must be tried once, ranked by its best
        # known expiry.
        now = datetime.now(timezone.utc)
        early = (now + timedelta(minutes=5)).isoformat()
        late = (now + timedelta(hours=9)).isoformat()

        def fake_read(read_id):
            exp = early if read_id.endswith("cli") else late
            return json.dumps({"accessToken": "same", "expiresAt": exp}).encode()

        with (
            patch("kiro_crew.hooks.safe_read_file_internal", side_effect=fake_read),
            patch.object(api, "_CLI_SQLITE_DBS", ()),
            patch.object(api, "_OTHER_SQLITE_DBS", ()),
        ):
            cands = api._candidate_tokens()
        assert [c.token for c in cands] == ["same"]

    def test_shared_profile_prefers_cli_store_over_fresher_sso_cache(self):
        # Two accounts in one IdC org share a profile ARN. The signed-in one's token
        # sits in kiro-cli's own store; the other account's leftover sits in a JSON
        # SSO cache and was refreshed LATER. Both clear the ARN proof, so the first
        # candidate wins: ranking by expiry alone served the other account's balance.
        # The readers are stubbed; the store paths stay where they are.
        now = datetime.now(timezone.utc)
        soon = now + timedelta(minutes=20)
        later = now + timedelta(hours=8)
        own_db = api._CLI_SQLITE_DBS[0]

        def fake_json(read_id, _now):
            return (
                ("other-account", later, None) if read_id == api._JSON_TOKEN_READ_IDS[0] else None
            )

        def fake_sqlite(db, _now):
            return ("signed-in", soon, None) if db == own_db else None

        shared_arn = "arn:aws:codewhisperer:us-east-1:1:profile/SHARED"
        usage = {
            "other-account": {
                "usageBreakdownList": [
                    {"resourceType": "CREDIT", "currentUsage": 10276.0, "usageLimit": 10000.0}
                ]
            },
            "signed-in": {
                "usageBreakdownList": [
                    {"resourceType": "CREDIT", "currentUsage": 214.0, "usageLimit": 10000.0}
                ]
            },
        }

        def fake_post(token, target, payload, **_kwargs):
            if target == api._TARGET_LIST_PROFILES:
                return _resp(200, {"profiles": [{"arn": shared_arn}]})
            return _resp(200, usage[token])

        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()
        try:
            with (
                patch.object(api, "_token_from_json", side_effect=fake_json),
                patch.object(api, "_token_from_sqlite", side_effect=fake_sqlite),
                patch.object(api, "_post", side_effect=fake_post),
            ):
                cands = api._candidate_tokens()
                out = api.fetch_usage_limits(expected_arn=shared_arn).usage
        finally:
            api._PROFILE_ARN_CACHE.clear()
            api._PROFILE_NAME_CACHE.clear()
        assert [c.token for c in cands] == ["signed-in", "other-account"]
        assert out is not None
        assert out["credits_used"] == 214.0, "served the other account's balance"

    def test_expiry_breaks_ties_within_the_same_provenance(self):
        now = datetime.now(timezone.utc)
        first, second = api._JSON_TOKEN_READ_IDS[0], api._JSON_TOKEN_READ_IDS[1]
        found = {
            first: ("stale-tok", now + timedelta(minutes=20), None),
            second: ("fresh-tok", now + timedelta(hours=8), None),
        }

        with (
            patch.object(
                api, "_token_from_json", side_effect=lambda read_id, _now: found.get(read_id)
            ),
            patch.object(api, "_token_from_sqlite", return_value=None),
        ):
            assert [c.token for c in api._candidate_tokens()] == ["fresh-tok", "stale-tok"]

    def test_expired_candidates_excluded(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        blob = json.dumps({"accessToken": "old", "expiresAt": past}).encode()
        with (
            patch("kiro_crew.hooks.safe_read_file_internal", return_value=blob),
            patch.object(api, "_CLI_SQLITE_DBS", ()),
            patch.object(api, "_OTHER_SQLITE_DBS", ()),
        ):
            assert api._candidate_tokens() == []


class TestUserIdOwnershipCheck:
    """IdC userId check: same ARN different userId is dropped, same userId is kept,
    unknown userId is ranked below a proven match, no proven match keeps today's order."""

    @pytest.fixture(autouse=True)
    def _clear_caches(self):
        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()
        yield
        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()

    def _usage_body(self, used: float) -> dict:
        return {
            "usageBreakdownList": [
                {"resourceType": "CREDIT", "currentUsage": used, "usageLimit": 10000.0}
            ]
        }

    def test_same_arn_different_user_id_drops_candidate(self):
        # The core userId ownership fix: two accounts share one profile ARN but carry
        # different IdC userIds. The wrong-account SSO cache is fresher; the
        # signed-in kiro-cli store carries the right userId. The SSO cache must
        # never be passed to GetUsageLimits.
        #
        # The kiro-cli candidate returns 500 for GetUsageLimits, so without the
        # userId filter the loop falls through to other-account and the assertion
        # below fails. With the filter, other-account is dropped and never tried.
        now = datetime.now(timezone.utc)
        shared_arn = "arn:aws:codewhisperer:us-east-1:1:profile/SHARED"
        signed_in_expiry = now + timedelta(minutes=30)
        other_expiry = now + timedelta(hours=8)  # fresher
        tokens_tried_for_usage = []

        def fake_json(read_id, _now):
            # SSO cache carries the other account's userId — same shared ARN
            if read_id == api._JSON_TOKEN_READ_IDS[0]:
                return ("other-account", other_expiry, "d-org.44488448")
            return None

        def fake_sqlite(db, _now):
            if db == api._CLI_SQLITE_DBS[0]:
                return ("signed-in", signed_in_expiry, "d-org.24a884d8")
            return None

        def fake_post(token, target, payload, **_kwargs):
            if target == api._TARGET_LIST_PROFILES:
                return _resp(200, {"profiles": [{"arn": shared_arn}]})
            tokens_tried_for_usage.append(token)
            if token == "signed-in":
                # kiro-cli candidate fails GetUsageLimits — forces the loop to
                # continue. Without the userId filter the next candidate
                # (other-account) would be tried; with the filter it is dropped.
                return _resp(500, {})
            return _resp(200, self._usage_body(10276.0))

        with (
            patch.object(api, "_token_from_json", side_effect=fake_json),
            patch.object(api, "_token_from_sqlite", side_effect=fake_sqlite),
            patch.object(api, "_post", side_effect=fake_post),
        ):
            api.fetch_usage_limits(expected_arn=shared_arn)
        # The SSO cache (other-account, wrong userId) must never reach GetUsageLimits.
        assert (
            "other-account" not in tokens_tried_for_usage
        ), "wrong-account SSO candidate was tried despite userId mismatch"

    def test_same_user_id_keeps_candidate(self):
        # A candidate whose userId matches the signed-in user is kept and served.
        now = datetime.now(timezone.utc)
        shared_arn = "arn:aws:codewhisperer:us-east-1:1:profile/SHARED"
        expiry = now + timedelta(hours=1)

        def fake_json(read_id, _now):
            if read_id == api._JSON_TOKEN_READ_IDS[0]:
                return ("sso-same-user", expiry, "d-org.24a884d8")
            return None

        def fake_sqlite(db, _now):
            if db == api._CLI_SQLITE_DBS[0]:
                # kiro-cli store also carries the same userId
                return ("cli-same-user", expiry, "d-org.24a884d8")
            return None

        usage = {
            "cli-same-user": self._usage_body(100.0),
            "sso-same-user": self._usage_body(100.0),
        }

        def fake_post(token, target, payload, **_kwargs):
            if target == api._TARGET_LIST_PROFILES:
                return _resp(200, {"profiles": [{"arn": shared_arn}]})
            return _resp(200, usage[token])

        with (
            patch.object(api, "_token_from_json", side_effect=fake_json),
            patch.object(api, "_token_from_sqlite", side_effect=fake_sqlite),
            patch.object(api, "_post", side_effect=fake_post),
        ):
            out = api.fetch_usage_limits(expected_arn=shared_arn)
        assert out.usage is not None
        assert out.usage["credits_used"] == 100.0

    def test_no_proven_match_keeps_kiro_cli_store_first(self):
        # When active_user_id cannot be determined (kiro-cli store blob has no
        # userId), no filtering is applied and the existing ranking stands.
        now = datetime.now(timezone.utc)
        shared_arn = "arn:aws:codewhisperer:us-east-1:1:profile/SHARED"
        cli_expiry = now + timedelta(minutes=30)
        sso_expiry = now + timedelta(hours=8)  # fresher but SSO

        def fake_json(read_id, _now):
            if read_id == api._JSON_TOKEN_READ_IDS[0]:
                return ("sso-no-uid", sso_expiry, None)  # no userId
            return None

        def fake_sqlite(db, _now):
            if db == api._CLI_SQLITE_DBS[0]:
                return ("cli-no-uid", cli_expiry, None)  # no userId
            return None

        usage = {
            "cli-no-uid": self._usage_body(214.0),
            "sso-no-uid": self._usage_body(10276.0),
        }

        def fake_post(token, target, payload, **_kwargs):
            if target == api._TARGET_LIST_PROFILES:
                return _resp(200, {"profiles": [{"arn": shared_arn}]})
            return _resp(200, usage[token])

        with (
            patch.object(api, "_token_from_json", side_effect=fake_json),
            patch.object(api, "_token_from_sqlite", side_effect=fake_sqlite),
            patch.object(api, "_post", side_effect=fake_post),
        ):
            out = api.fetch_usage_limits(expected_arn=shared_arn)
        # kiro-cli store first (today's behaviour): cli-no-uid is tried first
        assert out.usage is not None
        assert (
            out.usage["credits_used"] == 214.0
        ), "fell back to SSO candidate when kiro-cli store had no userId"


class TestProfileCacheCap:
    @pytest.fixture(autouse=True)
    def _clear_arn_cache(self):
        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()
        yield
        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()

    def test_cache_is_bounded(self):
        # Keyed by token digest and never expiring, so each profile switch adds
        # an entry — the cap keeps a long-lived gateway from accumulating them.
        for i in range(api._PROFILE_CACHE_MAX + 10):
            with patch.object(
                api, "_post", return_value=_resp(200, {"profiles": [{"arn": f"arn:{i}"}]})
            ):
                api._list_profile_arn(f"token-{i}")
        assert len(api._PROFILE_ARN_CACHE) <= api._PROFILE_CACHE_MAX
        assert len(api._PROFILE_NAME_CACHE) <= api._PROFILE_CACHE_MAX

    def test_both_caches_evict_together(self):
        # A display name must never outlive the ARN it belongs to, or it could be
        # shown next to a different account's credits.
        for i in range(api._PROFILE_CACHE_MAX + 5):
            with patch.object(
                api,
                "_post",
                return_value=_resp(
                    200, {"profiles": [{"arn": f"arn:{i}", "profileName": f"Org {i}"}]}
                ),
            ):
                api._list_profile_arn(f"token-{i}")
        assert set(api._PROFILE_ARN_CACHE) == set(api._PROFILE_NAME_CACHE)


class TestSafeReadFileInternalSymlink:
    """hooks.safe_read_file_internal must bind the read to the real allowlisted
    file (O_NOFOLLOW + fstat), never a symlink-redirected target."""

    def test_reads_regular_allowlisted_file(self, tmp_path, monkeypatch):
        from kiro_crew import hooks

        f = tmp_path / "tok.json"
        f.write_bytes(b'{"accessToken":"x"}')
        # An absolute allowlist value replaces the Path.home() prefix on join.
        monkeypatch.setitem(hooks._INTERNAL_READ_ALLOWLIST, "test.reg", str(f))
        monkeypatch.setattr(hooks, "is_sensitive_path", lambda p: True)
        monkeypatch.setattr(hooks, "_emit_internal_read_audit", lambda *a, **k: True)
        assert hooks.safe_read_file_internal("test.reg") == b'{"accessToken":"x"}'

    def test_rejects_symlinked_allowlisted_path(self, tmp_path, monkeypatch):
        from kiro_crew import hooks

        real = tmp_path / "real.json"
        real.write_bytes(b'{"accessToken":"x"}')
        link = tmp_path / "link.json"
        link.symlink_to(real)
        monkeypatch.setitem(hooks._INTERNAL_READ_ALLOWLIST, "test.symlink", str(link))
        monkeypatch.setattr(hooks, "is_sensitive_path", lambda p: True)
        monkeypatch.setattr(hooks, "_emit_internal_read_audit", lambda *a, **k: True)
        # O_NOFOLLOW makes the open fail on the symlink -> None, never the target.
        assert hooks.safe_read_file_internal("test.symlink") is None

    def test_rejects_read_id_not_in_allowlist(self, monkeypatch):
        """An unregistered read_id fails closed (CWE-1188): PermissionError with
        'not in allowlist' AND a 'not_allowlisted' SEL audit, never a read."""
        from kiro_crew import hooks

        audited = []
        monkeypatch.setattr(
            hooks,
            "_emit_internal_read_audit",
            lambda read_id, outcome: audited.append((read_id, outcome)) or True,
        )
        with pytest.raises(PermissionError, match="not in allowlist"):
            hooks.safe_read_file_internal("rogue.unregistered.id")
        assert ("rogue.unregistered.id", "not_allowlisted") in audited

    def test_rejects_allowlisted_but_non_sensitive_path(self, tmp_path, monkeypatch):
        """A registered read_id whose resolved path is NOT sensitive fails closed
        (CWE-1188): the carve-out is only valid for a sensitive path, so drift is
        refused with 'non-sensitive path' + a 'not_sensitive' SEL audit. Mirrors
        the happy-path registration (setitem on _INTERNAL_READ_ALLOWLIST) but
        stubs is_sensitive_path to False so the defense-in-depth check trips."""
        from kiro_crew import hooks

        f = tmp_path / "tok.json"
        f.write_bytes(b'{"accessToken":"x"}')
        monkeypatch.setitem(hooks._INTERNAL_READ_ALLOWLIST, "test.nonsensitive", str(f))
        monkeypatch.setattr(hooks, "is_sensitive_path", lambda p: False)
        audited = []
        monkeypatch.setattr(
            hooks,
            "_emit_internal_read_audit",
            lambda read_id, outcome: audited.append((read_id, outcome)) or True,
        )
        with pytest.raises(PermissionError, match="non-sensitive path"):
            hooks.safe_read_file_internal("test.nonsensitive")
        assert ("test.nonsensitive", "not_sensitive") in audited


class TestTokenStoreSensitivePath:
    """The kiro-cli/amazon-q SQLite token stores are classified sensitive so
    agent file tools cannot read them through the shared gate."""

    def test_sqlite_token_stores_are_sensitive(self):
        from pathlib import Path

        from kiro_crew.security import is_sensitive_path

        home = Path.home()
        for base in (
            ".local/share",
            "Library/Application Support",
            "AppData/Local",
            "AppData/Roaming",
        ):
            for app in ("kiro-cli", "amazon-q"):
                # The DB and its WAL/SHM/journal sidecars must all be sensitive.
                assert is_sensitive_path(str(home / base / app / "data.sqlite3"))
                assert is_sensitive_path(str(home / base / app / "data.sqlite3-wal"))


class TestExpectedArnSelection:
    """The profile probe answers a question ("is the signed-in profile in this
    token's list?"), never a position ("what is first?").

    The failure mode being locked out: taking the FIRST entry carrying an
    ARN means that, for a token entitled to two or more profiles, whether the
    credit pill works depends on the order the upstream API happens to
    return — the signed-in profile can be present in the list and never asked
    about. A one-answer-per-token memo then makes that wrong selection sticky
    for the process lifetime.
    """

    @pytest.fixture(autouse=True)
    def _clear_arn_cache(self):
        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()
        yield
        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()

    ARN_A = "arn:aws:codewhisperer:us-east-1:1:profile/A"
    ARN_B = "arn:aws:codewhisperer:us-east-1:2:profile/B"
    USAGE = {
        "usageBreakdownList": [
            {"resourceType": "CREDIT", "currentUsage": 1200.0, "usageLimit": 10000.0}
        ]
    }

    def _post_two_profiles(self, token, target, payload, **_kwargs):
        # The signed-in profile (A) is SECOND — exactly the reporter's shape.
        if target == api._TARGET_LIST_PROFILES:
            return _resp(
                200,
                {
                    "profiles": [
                        {"arn": self.ARN_B, "profileName": "Other Org"},
                        {"arn": self.ARN_A, "profileName": "My Org"},
                    ]
                },
            )
        return _resp(200, self.USAGE)

    def test_expected_arn_second_in_list_is_matched(self):
        # THE bug: a signed-in profile that is present but not first must still
        # be found, and the pill must show real usage instead of degrading.
        with (
            patch.object(api, "_candidate_tokens", return_value=[_cand("tok")]),
            patch.object(api, "_post", side_effect=self._post_two_profiles),
        ):
            out = api.fetch_usage_limits(expected_arn=self.ARN_A).usage
        assert out is not None, "credential declined although the signed-in profile is listed"
        assert out["_profile_arn"] == self.ARN_A
        assert out["credits_used"] == 1200.0

    def test_matched_profile_name_rides_along(self):
        # The ``account`` label must be the MATCHED profile's name, not the
        # first entry's — a wrong label attributes the credits to another org.
        with (
            patch.object(api, "_candidate_tokens", return_value=[_cand("tok")]),
            patch.object(api, "_post", side_effect=self._post_two_profiles),
        ):
            out = api.fetch_usage_limits(expected_arn=self.ARN_A).usage
        assert out is not None
        assert out.get("account") == "My Org"

    def test_expected_arn_absent_declines_the_credential(self):
        # A multi-profile list WITHOUT the expected ARN proves nothing: the
        # credential must be declined without spending a GetUsageLimits call —
        # presence is the proof, and mere listing of OTHER profiles is not.
        seen: list[str] = []

        def recording(token, target, payload, **_kwargs):
            seen.append(target)
            if target == api._TARGET_LIST_PROFILES:
                return _resp(200, {"profiles": [{"arn": self.ARN_B}, {"arn": "arn:other"}]})
            return _resp(200, self.USAGE)

        with (
            patch.object(api, "_candidate_tokens", return_value=[_cand("tok")]),
            patch.object(api, "_post", side_effect=recording),
        ):
            assert api.fetch_usage_limits(expected_arn=self.ARN_A).usage is None
        assert api._TARGET_GET_USAGE not in seen

    def test_single_profile_path_unchanged(self):
        def fake_post(token, target, payload, **_kwargs):
            if target == api._TARGET_LIST_PROFILES:
                return _resp(200, {"profiles": [{"arn": self.ARN_A}]})
            return _resp(200, self.USAGE)

        with (
            patch.object(api, "_candidate_tokens", return_value=[_cand("tok")]),
            patch.object(api, "_post", side_effect=fake_post),
        ):
            out = api.fetch_usage_limits(expected_arn=self.ARN_A).usage
        assert out is not None
        assert out["_profile_arn"] == self.ARN_A

    def test_no_expected_arn_keeps_first_with_arn(self):
        # Source-anchored mode asks no question, so the first entry carrying an
        # ARN is still the answer (the provenance check is the proof there).
        with patch.object(api, "_post", side_effect=self._post_two_profiles):
            assert api._list_profile_arn("tok") == self.ARN_B

    def test_warm_cache_does_not_cross_expectations(self):
        # Two probes with the same token and different expected ARNs must not
        # return each other's answer: one memoized ARN per token digest made
        # the wrong first selection sticky for the process lifetime.
        with (
            patch.object(api, "_candidate_tokens", return_value=[_cand("tok")]),
            patch.object(api, "_post", side_effect=self._post_two_profiles),
        ):
            first = api.fetch_usage_limits(expected_arn=self.ARN_B).usage
            second = api.fetch_usage_limits(expected_arn=self.ARN_A).usage
        assert first is not None
        assert first["_profile_arn"] == self.ARN_B
        assert second is not None, "warm cache served the other expectation's answer"
        assert second["_profile_arn"] == self.ARN_A

    def test_name_cache_keyed_with_the_arn_it_belongs_to(self):
        # The co-cached display name must follow the same composite key, so an
        # account label can never be served for a different expectation.
        with patch.object(api, "_post", side_effect=self._post_two_profiles):
            assert api._list_profile_arn("tok", expected_arn=self.ARN_A) == self.ARN_A
            assert api._list_profile_arn("tok", expected_arn=self.ARN_B) == self.ARN_B
        assert api._account_name("tok", self.ARN_A) == "My Org"
        assert api._account_name("tok", self.ARN_B) == "Other Org"

    def test_anchored_miss_is_not_cached(self):
        # An absent expected ARN may be post-login propagation lag; the next
        # refresh must re-probe rather than serve a pinned miss.
        with patch.object(
            api, "_post", return_value=_resp(200, {"profiles": [{"arn": self.ARN_B}]})
        ):
            assert api._list_profile_arn("tok", expected_arn=self.ARN_A) is None
        with patch.object(api, "_post", side_effect=self._post_two_profiles) as mp:
            assert api._list_profile_arn("tok", expected_arn=self.ARN_A) == self.ARN_A
        assert mp.call_count == 1

    def test_anchored_match_is_memoized(self):
        # The composite key still saves the round-trip on the SAME question.
        with patch.object(api, "_post", side_effect=self._post_two_profiles) as mp:
            assert api._list_profile_arn("tok", expected_arn=self.ARN_A) == self.ARN_A
            assert api._list_profile_arn("tok", expected_arn=self.ARN_A) == self.ARN_A
        assert mp.call_count == 1

    def test_empty_anchor_behaves_as_no_anchor_in_probe(self):
        # A falsy anchor means "no question asked" — first-with-ARN, matching
        # fetch_usage_limits' own truthiness convention for expected_arn.
        with patch.object(api, "_post", side_effect=self._post_two_profiles):
            assert api._list_profile_arn("tok", expected_arn="") == self.ARN_B


class TestExternalIdpTokenType:
    """An External IdP token carries ``TokenType: EXTERNAL_IDP`` on both control-plane calls."""

    ARN = "arn:aws:codewhisperer:us-east-1:1:profile/p"

    @pytest.fixture(autouse=True)
    def _clear_caches(self):
        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()
        yield
        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()

    @staticmethod
    def _capture_open(captured: list):
        class _FakeResp:
            status = 200

            def read(self, n=None):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(self, req, timeout=None):
            captured.append(req)
            return _FakeResp()

        return fake_open

    def test_post_sends_the_token_type_header(self):
        captured: list = []
        with patch("urllib.request.OpenerDirector.open", self._capture_open(captured)):
            api._post("tok", api._TARGET_GET_USAGE, {}, token_type=api.TOKEN_TYPE_EXTERNAL_IDP)
        # urllib stores header keys capitalized (first letter only).
        assert captured[0].get_header("Tokentype") == "EXTERNAL_IDP"

    def test_post_without_a_token_type_sends_no_header(self):
        captured: list = []
        with patch("urllib.request.OpenerDirector.open", self._capture_open(captured)):
            api._post("tok", api._TARGET_GET_USAGE, {})
        assert captured[0].get_header("Tokentype") is None

    @staticmethod
    def _store(tmp_path, key: str):
        db = tmp_path / "data.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        con.execute(
            "INSERT INTO auth_kv VALUES (?, ?)",
            (key, json.dumps({"access_token": "live", "expires_at": future})),
        )
        con.commit()
        con.close()
        return db

    @pytest.mark.parametrize(
        "key, token_type",
        [("kirocli:external-idp:token", "EXTERNAL_IDP"), ("kirocli:odic:token", "")],
    )
    def test_store_key_decides_the_token_type(self, tmp_path, key, token_type):
        db = self._store(tmp_path, key)
        with (
            patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True),
            patch.object(api, "_token_from_json", return_value=None),
            patch.object(api, "_CLI_SQLITE_DBS", (db,)),
            patch.object(api, "_OTHER_SQLITE_DBS", ()),
        ):
            found = api._token_from_sqlite(db, datetime.now(timezone.utc))
            cands = api._candidate_tokens()
        assert found is not None and found.token_type == token_type
        assert [(c.token, c.token_type) for c in cands] == [("live", token_type)]

    def _fetch(self, token_type: str) -> list:
        sent: list = []

        def fake_post(token, target, payload, *, endpoint=None, token_type=""):
            sent.append((target, token_type))
            if target == api._TARGET_LIST_PROFILES:
                return _resp(200, {"profiles": [{"arn": self.ARN}]})
            return _resp(
                200,
                {
                    "usageBreakdownList": [
                        {"resourceType": "CREDIT", "currentUsage": 1.0, "usageLimit": 10.0}
                    ]
                },
            )

        soon = datetime.now(timezone.utc) + timedelta(hours=1)
        hit = api._StoreToken("idp-tok", soon, None, token_type)
        with (
            patch.object(api, "_token_from_json", return_value=None),
            patch.object(
                api,
                "_token_from_sqlite",
                side_effect=lambda db, _n: hit if db == api._CLI_SQLITE_DBS[0] else None,
            ),
            patch.object(api, "_post", side_effect=fake_post),
        ):
            assert api.fetch_usage_limits(expected_arn=self.ARN).usage is not None
        return sent

    def test_external_idp_candidate_sends_it_on_both_calls(self):
        sent = self._fetch(api.TOKEN_TYPE_EXTERNAL_IDP)
        assert {t for t, _ in sent} == {api._TARGET_LIST_PROFILES, api._TARGET_GET_USAGE}
        assert all(tt == "EXTERNAL_IDP" for _, tt in sent)

    def test_other_candidate_sends_none(self):
        sent = self._fetch("")
        assert {t for t, _ in sent} == {api._TARGET_LIST_PROFILES, api._TARGET_GET_USAGE}
        assert all(tt == "" for _, tt in sent)
