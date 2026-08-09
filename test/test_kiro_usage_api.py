"""Tests for kiro_crew.dashboard.handlers.kiro_usage_api — the direct RTS
GetUsageLimits client that surfaces real Kiro credit usage/overage.
"""
from __future__ import annotations

import json
import sqlite3
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.dashboard.handlers.kiro_usage_api as api


def _resp(status: int, body: object) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    if isinstance(body, Exception):
        r.json.side_effect = body
    else:
        r.json.return_value = body
    return r


def _cand(value: str, *, own: bool = True, hours: float = 1.0):
    """Build a credential candidate for tests.

    ``own`` defaults to True (kiro-cli's own auth store) so ARN-anchored tests stay
    focused on the ARN check rather than incidentally tripping the provenance one.
    Pass ``own=False`` for the JSON SSO caches / another product's store.
    """
    expiry = datetime.now(timezone.utc) + timedelta(hours=hours)
    return api._Candidate(value, expiry, own)


class TestBounded:
    def test_valid(self):
        assert api._bounded("42.5") == 42.5
        assert api._bounded(0) == 0.0

    def test_rejects_nan_inf_negative_and_oversized(self):
        assert api._bounded(float("nan")) is None
        assert api._bounded(float("inf")) is None
        assert api._bounded(-1) is None
        assert api._bounded(api._MAX_CREDITS + 1) is None

    def test_rejects_non_numeric(self):
        assert api._bounded("abc") is None
        assert api._bounded(None) is None


class TestMapResponse:
    def _credit(self, **kw):
        base = {"resourceType": "CREDIT", "currentUsage": 29527.0, "usageLimit": 10000.0}
        base.update(kw)
        return {"usageBreakdownList": [base]}

    def test_maps_total_limit_overage(self):
        out = api._map_response(self._credit(currentOverages=19527.0,
                                             overageRate=0.04, overageCharges=781.08))
        assert out["credits_used"] == 29527.0
        assert out["credits_plan"] == 10000.0
        assert out["credits_overage"] == 19527.0
        assert out["credits_covered"] == 10000.0  # min(used, plan)
        assert out["percentage"] == 295.3
        assert out["overage_rate"] == 0.04
        assert out["cost_usd"] == 781.08
        assert out["source"] == "api"

    def test_computes_overage_when_absent(self):
        out = api._map_response(self._credit())  # no currentOverages
        assert out["credits_overage"] == 19527.0  # max(0, used - plan)

    def test_prefers_with_precision_fields(self):
        out = api._map_response(self._credit(currentUsageWithPrecision=29527.12,
                                             usageLimitWithPrecision=10000.0))
        assert out["credits_used"] == 29527.12

    def test_null_precision_falls_back_to_legacy_fields(self):
        # A present-but-null precision value must not suppress the legacy field.
        out = api._map_response(self._credit(currentUsageWithPrecision=None,
                                             usageLimitWithPrecision=None))
        assert out["credits_used"] == 29527.0
        assert out["credits_plan"] == 10000.0

    def test_picks_credit_among_multiple_breakdowns(self):
        data = {"usageBreakdownList": [
            {"resourceType": "OTHER", "currentUsage": 1, "usageLimit": 2},
            {"resourceType": "CREDIT", "currentUsage": 5, "usageLimit": 100},
        ]}
        assert api._map_response(data)["credits_used"] == 5.0

    def test_falls_back_to_first_breakdown(self):
        data = {"usageBreakdownList": [{"currentUsage": 5, "usageLimit": 100}]}
        assert api._map_response(data)["credits_used"] == 5.0

    def test_none_when_only_typed_non_credit_breakdown(self):
        # A typed non-CREDIT entry (e.g. TOKEN) must NOT be shown as credits.
        data = {"usageBreakdownList": [
            {"resourceType": "TOKEN", "currentUsage": 5, "usageLimit": 100}]}
        assert api._map_response(data) is None

    def test_none_when_no_breakdown(self):
        assert api._map_response({"usageBreakdownList": []}) is None
        assert api._map_response({}) is None

    def test_extracts_bonus_pool(self):
        # A bonus/free-trial breakdown alongside CREDIT is surfaced separately.
        data = {"usageBreakdownList": [
            {"resourceType": "CREDIT", "currentUsage": 41.0, "usageLimit": 1000.0},
            {"resourceType": "FREE_TRIAL", "currentUsage": 386.34, "usageLimit": 500.0},
        ]}
        out = api._map_response(data)
        assert out["credits_plan"] == 1000.0
        assert out["bonus_used"] == 386.34
        assert out["bonus_limit"] == 500.0
        assert out["bonus_label"] == "Free Trial"

    def test_ignores_non_bonus_secondary_breakdown(self):
        # A TOKEN quota alongside CREDIT must NOT be mistaken for a bonus pool.
        data = {"usageBreakdownList": [
            {"resourceType": "CREDIT", "currentUsage": 5, "usageLimit": 100},
            {"resourceType": "TOKEN", "currentUsage": 7, "usageLimit": 50},
        ]}
        assert "bonus_limit" not in api._map_response(data)

    def test_none_when_response_not_dict(self):
        assert api._map_response([]) is None
        assert api._map_response(None) is None

    def test_skips_non_dict_breakdown_entries(self):
        # A malformed [null] / mixed list must not raise — non-dicts are dropped.
        data = {"usageBreakdownList": [None, {"resourceType": "CREDIT",
                                              "currentUsage": 5, "usageLimit": 100}]}
        assert api._map_response(data)["credits_used"] == 5.0
        assert api._map_response({"usageBreakdownList": [None, 3, "x"]}) is None

    def test_non_dict_subscription_info_ignored(self):
        data = {"usageBreakdownList": [{"resourceType": "CREDIT",
                                        "currentUsage": 5, "usageLimit": 100}],
                "subscriptionInfo": []}
        assert api._map_response(data)["credits_used"] == 5.0  # no crash, no plan name

    def test_none_when_limit_zero_or_missing(self):
        assert api._map_response({"usageBreakdownList": [
            {"resourceType": "CREDIT", "currentUsage": 5, "usageLimit": 0}]}) is None

    def test_plan_and_reset_date(self):
        data = self._credit()
        data["subscriptionInfo"] = {"subscriptionTitle": "KIRO POWER"}
        data["nextDateReset"] = 1785000000  # epoch seconds
        out = api._map_response(data)
        assert out["plan"] == "KIRO POWER"
        assert out["resets"]  # formatted YYYY-MM-DD string present

    def test_non_string_plan_name_is_dropped(self):
        # An object/array subscriptionTitle must not reach the cache/UI.
        data = self._credit()
        data["subscriptionInfo"] = {"subscriptionTitle": {"nested": "obj"}}
        assert "plan" not in api._map_response(data)

    def test_string_plan_name_is_bounded(self):
        data = self._credit()
        data["subscriptionInfo"] = {"subscriptionTitle": "X" * 500}
        assert api._map_response(data)["plan"] == "X" * 100


class TestParseIso:
    def test_handles_nanosecond_precision(self):
        # kiro-cli emits 9 fractional digits; fromisoformat accepts <=6.
        dt = api._parse_iso("2026-07-06T20:08:42.478986819Z")
        assert dt is not None
        assert dt.year == 2026 and dt.month == 7 and dt.day == 6
        assert dt.tzinfo is not None

    def test_expired_nanosecond_token_is_detected(self):
        # The bug this fixes: an expired ns-precision expiry must NOT be treated
        # as non-expiring. _unexpired must return False for a past ns timestamp.
        now = datetime.now(timezone.utc)
        assert api._unexpired("2026-06-05T01:41:25.464911393Z", now) is False

    def test_missing_expiry_is_rejected(self):
        # Deny-by-default: no expiry field -> token rejected, not non-expiring.
        now = datetime.now(timezone.utc)
        assert api._unexpired(None, now) is False
        assert api._unexpired("", now) is False

    def test_unparseable_expiry_is_rejected(self):
        # Deny-by-default: garbled expiry -> reject; the candidate loop moves on.
        now = datetime.now(timezone.utc)
        assert api._unexpired("not-a-date", now) is False


class TestLoadBearerToken:
    def test_reads_access_token_from_json(self):
        # JSON reads go through hooks.safe_read_file_internal (sensitive path).
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

        def fake_read(read_id):
            return (json.dumps({"accessToken": "tok-abc", "expiresAt": future}).encode()
                    if read_id == "kiro_usage_api.sso_token_cli" else None)
        with patch("kiro_crew.hooks.safe_read_file_internal", side_effect=fake_read), \
             patch.object(api, "_CLI_SQLITE_DBS", ()), \
             patch.object(api, "_OTHER_SQLITE_DBS", ()):
            assert api._load_bearer_token() == "tok-abc"

    def test_skips_expired_json_token(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        blob = json.dumps({"accessToken": "old", "expiresAt": past}).encode()
        with patch("kiro_crew.hooks.safe_read_file_internal", return_value=blob), \
             patch.object(api, "_CLI_SQLITE_DBS", ()), \
             patch.object(api, "_OTHER_SQLITE_DBS", ()):
            assert api._load_bearer_token() is None

    def test_accepts_future_json_token(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

        def fake_read(read_id):
            return (json.dumps({"accessToken": "fresh", "expiresAt": future}).encode()
                    if read_id.endswith("cli") else None)
        with patch("kiro_crew.hooks.safe_read_file_internal", side_effect=fake_read), \
             patch.object(api, "_CLI_SQLITE_DBS", ()), \
             patch.object(api, "_OTHER_SQLITE_DBS", ()):
            assert api._load_bearer_token() == "fresh"

    def test_missing_sources_return_none(self, tmp_path):
        with patch("kiro_crew.hooks.safe_read_file_internal", return_value=None), \
             patch.object(api, "_CLI_SQLITE_DBS", (tmp_path / "nope.sqlite3",)), \
             patch.object(api, "_OTHER_SQLITE_DBS", ()):
            assert api._load_bearer_token() is None

    def test_falls_through_expired_json_to_sqlite(self, tmp_path):
        # Stale JSON (expired) must be skipped and the live SQLite token used —
        # exactly the Linux clouddesk situation.
        past = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        stale = json.dumps({"accessToken": "stale", "expiresAt": past}).encode()
        db = tmp_path / "data.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        con.execute(
            "INSERT INTO auth_kv VALUES (?, ?)",
            ("kirocli:odic:token",
             json.dumps({"access_token": "live-sqlite", "expires_at": future})),
        )
        con.commit()
        con.close()
        with patch("kiro_crew.hooks.safe_read_file_internal", return_value=stale), \
             patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True), \
             patch.object(api, "_CLI_SQLITE_DBS", (db,)), \
             patch.object(api, "_OTHER_SQLITE_DBS", ()):
            assert api._load_bearer_token() == "live-sqlite"

    def test_sqlite_token_denied_when_audit_cannot_be_recorded(self, tmp_path):
        # Fail closed: a live token whose SEL audit cannot be persisted must NOT
        # be returned — the caller degrades to the text scrape instead.
        db = tmp_path / "data.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        con.execute(
            "INSERT INTO auth_kv VALUES (?, ?)",
            ("kirocli:odic:token",
             json.dumps({"access_token": "live-sqlite", "expires_at": future})),
        )
        con.commit()
        con.close()
        with patch("kiro_crew.hooks.safe_read_file_internal", return_value=None), \
             patch("kiro_crew.hooks.emit_internal_read_audit", return_value=False), \
             patch.object(api, "_CLI_SQLITE_DBS", (db,)), \
             patch.object(api, "_OTHER_SQLITE_DBS", ()):
            assert api._load_bearer_token() is None

    def test_skips_expired_sqlite_token(self, tmp_path):
        db = tmp_path / "data.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        con.execute(
            "INSERT INTO auth_kv VALUES (?, ?)",
            ("kirocli:odic:token",
             json.dumps({"access_token": "old", "expires_at": past})),
        )
        con.commit()
        con.close()
        with patch("kiro_crew.hooks.safe_read_file_internal", return_value=None), \
             patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True) as audit, \
             patch.object(api, "_CLI_SQLITE_DBS", (db,)), \
             patch.object(api, "_OTHER_SQLITE_DBS", ()):
            assert api._load_bearer_token() is None
        # The credential blob WAS read even though expired — that access must
        # be recorded so the audit trail covers every read of the store.
        audit.assert_any_call(api._SQLITE_AUDIT_READ_ID, "expired")

    def test_unregistered_audit_read_id_is_rejected(self):
        # The audit entry point enforces its own registry -- an unregistered
        # read_id must be refused (False), so it cannot serve as an unscoped
        # bypass of the SEL-audit surface, and callers fail closed on it.
        from kiro_crew import hooks
        assert hooks.emit_internal_read_audit("rogue.read_id", "success") is False
        assert "kiro_usage_api.sqlite_token" in hooks._AUDIT_ONLY_READ_IDS

    def test_sqlite_oserror_fails_closed(self, tmp_path):
        # An OSError from db.exists() (e.g. permission-denied parent dir) is not
        # a sqlite3.Error — it must still fail closed to None, never raise.
        db = tmp_path / "data.sqlite3"

        class _RaisingPath(type(db)):  # pathlib.Path subclass
            def exists(self):
                raise OSError("permission denied")

        raising = _RaisingPath(db)
        with patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True):
            now = datetime.now(timezone.utc)
            assert api._token_from_sqlite(raising, now) is None

    def test_json_token_non_dict_ignored(self):
        # A JSON list/scalar cache entry must not raise on .get — it fails closed
        # (None) without aborting the remaining candidate sources.
        with patch("kiro_crew.hooks.safe_read_file_internal", return_value=b"[]"), \
             patch.object(api, "_CLI_SQLITE_DBS", ()), \
             patch.object(api, "_OTHER_SQLITE_DBS", ()):
            assert api._load_bearer_token() is None

    def test_symlinked_sqlite_db_is_rejected(self, tmp_path):
        # A symlinked DB path must be refused so the read cannot be redirected.
        real = tmp_path / "data.sqlite3"
        con = sqlite3.connect(str(real))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        con.commit()
        con.close()
        link = tmp_path / "link.sqlite3"
        link.symlink_to(real)
        with patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True):
            assert api._token_from_sqlite(link, datetime.now(timezone.utc)) is None

    def test_sqlite_opened_but_no_token_still_audits(self, tmp_path):
        # Audit-on-every-outcome: an opened DB with no matching token row must
        # still record one audit outcome, not fall through silently.
        db = tmp_path / "data.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        con.commit()
        con.close()
        with patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True) as audit:
            assert api._token_from_sqlite(db, datetime.now(timezone.utc)) is None
        audit.assert_any_call(api._SQLITE_AUDIT_READ_ID, "no_token")

    def test_social_login_token_key_is_recognized(self, tmp_path):
        # GitHub social login stores its bearer token under a different key
        # (kirocli:social:token) than the OIDC flow (kirocli:odic:token).
        # The API module must read it from kiro-cli's own store.
        db = tmp_path / "data.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        con.execute(
            "INSERT INTO auth_kv VALUES (?, ?)",
            ("kirocli:social:token",
             json.dumps({"access_token": "social-tok", "expires_at": future})),
        )
        con.commit()
        con.close()
        with patch("kiro_crew.hooks.safe_read_file_internal", return_value=None), \
             patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True), \
             patch.object(api, "_CLI_SQLITE_DBS", (db,)), \
             patch.object(api, "_OTHER_SQLITE_DBS", ()):
            assert api._load_bearer_token() == "social-tok"

    def test_external_idp_token_key_is_recognized(self, tmp_path):
        # Identity Center (org/enterprise SSO) stores its bearer token under
        # kirocli:external-idp:token. On Linux the JSON SSO cache is not
        # refreshed, so this SQLite key is the only live source.
        db = tmp_path / "data.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        con.execute(
            "INSERT INTO auth_kv VALUES (?, ?)",
            ("kirocli:external-idp:token",
             json.dumps({"access_token": "idp-tok", "expires_at": future})),
        )
        con.commit()
        con.close()
        with patch("kiro_crew.hooks.safe_read_file_internal", return_value=None), \
             patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True), \
             patch.object(api, "_CLI_SQLITE_DBS", (db,)), \
             patch.object(api, "_OTHER_SQLITE_DBS", ()):
            assert api._load_bearer_token() == "idp-tok"


class TestPostSecurityControls:
    def test_post_sets_headers_method_and_url(self):
        captured = {}

        class _FakeResp:
            status = 200

            def read(self, n=None):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(self, req, timeout=None):
            captured["req"] = req
            captured["timeout"] = timeout
            return _FakeResp()

        with patch("urllib.request.OpenerDirector.open", fake_open):
            api._post("tok-xyz", api._TARGET_GET_USAGE, {"origin": "AI_EDITOR"})
        req = captured["req"]
        assert req.get_full_url() == api._RTS_ENDPOINT             # hardcoded endpoint
        assert req.get_method() == "POST"
        assert req.data == json.dumps({"origin": "AI_EDITOR"}).encode()
        # urllib stores header keys capitalized (first letter only).
        assert req.get_header("Authorization") == "Bearer tok-xyz"
        assert req.get_header("X-amz-target") == api._TARGET_GET_USAGE
        assert req.get_header("User-agent")
        assert captured["timeout"] == api._TIMEOUT_SECS

    def test_opener_verifies_tls_and_disables_redirects(self):
        opener = api._build_opener()
        # Redirects disabled: our _NoRedirect handler is installed (control 3).
        assert any(isinstance(h, api._NoRedirect) for h in opener.handlers)
        https = [h for h in opener.handlers
                 if isinstance(h, urllib.request.HTTPSHandler)]
        assert https, "HTTPSHandler present"
        ctx = https[0]._context
        assert ctx.verify_mode == ssl.CERT_REQUIRED   # TLS verification on (control 2)
        assert ctx.check_hostname is True

    def test_http_error_is_returned_as_response_not_raised(self):
        # A 403 must come back as a _Resp (status_code=403), not raise — so the
        # caller can branch on status and fail over to the next token.
        err = urllib.error.HTTPError(api._RTS_ENDPOINT, 403, "Forbidden", {}, None)
        with patch("urllib.request.OpenerDirector.open", side_effect=err):
            r = api._post("tok", api._TARGET_GET_USAGE, {})
        assert r.status_code == 403

    def test_transport_error_raises_request_error(self):
        with patch("urllib.request.OpenerDirector.open",
                   side_effect=urllib.error.URLError("dns")):
            with pytest.raises(api._RequestError):
                api._post("tok", api._TARGET_GET_USAGE, {})

    def test_oversized_body_fails_closed(self):
        # A body above _MAX_RESP_BYTES is discarded (empty text -> {} on json())
        # rather than buffered, so an oversized/streamed response can't exhaust
        # memory; the caller then falls back to the text scrape.
        class _Big:
            status = 200

            def read(self, n):
                return b"x" * n  # returns cap+1 bytes -> over the limit

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(self, req, timeout=None):
            return _Big()

        with patch("urllib.request.OpenerDirector.open", fake_open):
            r = api._post("tok", api._TARGET_GET_USAGE, {})
        assert r.status_code == 200
        assert r.json() == {}


class TestFetchUsageLimits:
    @pytest.fixture(autouse=True)
    def _clear_arn_cache(self):
        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()
        yield
        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()

    def test_surfaces_account_from_profile_name(self):
        # The signed-in account's profileName from ListAvailableProfiles must be
        # surfaced as the usage dict's ``account`` field.
        usage_body = {"usageBreakdownList": [
            {"resourceType": "CREDIT", "currentUsage": 5.0, "usageLimit": 100.0}]}
        arn = "arn:aws:codewhisperer:...:profile/X"

        def fake_post(token, target, payload):
            if target == api._TARGET_LIST_PROFILES:
                return _resp(200, {"profiles": [
                    {"arn": arn,
                     "profileName": "Acme Corp"}]})
            return _resp(200, usage_body)

        with patch.object(api, "_candidate_tokens", return_value=[_cand("tok")]), \
             patch.object(api, "_post", side_effect=fake_post):
            out = api.fetch_usage_limits(expected_arn=arn)
        assert out["account"] == "Acme Corp"

    def test_no_account_when_profile_has_no_name(self):
        # An org profile with an ARN but no profileName must not set ``account``
        # (individual Builder ID accounts likewise have no profile at all).
        usage_body = {"usageBreakdownList": [
            {"resourceType": "CREDIT", "currentUsage": 5.0, "usageLimit": 100.0}]}

        def fake_post(token, target, payload):
            if target == api._TARGET_LIST_PROFILES:
                return _resp(200, {"profiles": [{"arn": "arn:x"}]})
            return _resp(200, usage_body)

        with patch.object(api, "_candidate_tokens", return_value=[_cand("tok")]), \
             patch.object(api, "_post", side_effect=fake_post):
            out = api.fetch_usage_limits(expected_arn="arn:x")
        assert "account" not in out

    def test_rejected_token_falls_over_to_next(self):
        # An unexpired-but-rejected first token must not shadow a working one.
        usage_body = {"usageBreakdownList": [
            {"resourceType": "CREDIT", "currentUsage": 5.0, "usageLimit": 100.0}]}
        arn = "arn:aws:codewhisperer:us-east-1:1:profile/A"

        def fake_post(token, target, payload):
            if target == api._TARGET_LIST_PROFILES:
                return _resp(200, {"profiles": [{"arn": arn}]})
            return _resp(403, {}) if token == "stale" else _resp(200, usage_body)

        with patch.object(api, "_candidate_tokens", return_value=[_cand("stale"), _cand("live")]), \
             patch.object(api, "_post", side_effect=fake_post):
            out = api.fetch_usage_limits(expected_arn=arn)
        assert out is not None
        assert out["credits_used"] == 5.0

    def test_all_tokens_rejected_fails_closed(self):
        # A matching ARN so the candidates are ELIGIBLE and the 403 is what
        # rejects them -- with a null ARN they would be skipped before _post and
        # this would pass without exercising the rejection path at all.
        with patch.object(api, "_candidate_tokens", return_value=[_cand("a"), _cand("b")]), \
             patch.object(api, "_list_profile_arn", return_value="arn:x"), \
             patch.object(api, "_post", return_value=_resp(403, {"reason": "FEATURE_NOT_SUPPORTED"})):
            assert api.fetch_usage_limits(expected_arn="arn:x") is None

    def test_request_exception_fails_closed(self):
        with patch.object(api, "_candidate_tokens", return_value=[_cand("tok")]), \
             patch.object(api, "_list_profile_arn", return_value="arn:x"), \
             patch.object(api, "_post", side_effect=api._RequestError("boom")):
            assert api.fetch_usage_limits(expected_arn="arn:x") is None

    def test_non_json_body_fails_closed(self):
        with patch.object(api, "_candidate_tokens", return_value=[_cand("tok")]), \
             patch.object(api, "_list_profile_arn", return_value="arn:x"), \
             patch.object(api, "_post", return_value=_resp(200, ValueError("not json"))):
            assert api.fetch_usage_limits(expected_arn="arn:x") is None

    def test_unexpected_error_fails_closed_not_raised(self):
        # An unforeseen exception for one token must be swallowed (fall back to
        # the text scrape), never propagate to _fetch_usage_bg.
        with patch.object(api, "_candidate_tokens", return_value=[_cand("tok")]), \
             patch.object(api, "_list_profile_arn", side_effect=RuntimeError("boom")):
            assert api.fetch_usage_limits(expected_arn="arn:x") is None

    def test_aggregate_deadline_short_circuits(self):
        # Once the wall-clock budget is exhausted, no further tokens are tried —
        # the caller degrades to the text scrape instead of spinning for minutes.
        with patch.object(api, "_TOTAL_DEADLINE_SECS", -1), \
             patch.object(api, "_candidate_tokens", return_value=[_cand("a"), _cand("b")]), \
             patch.object(api, "_post") as mp:
            assert api.fetch_usage_limits(expected_arn="arn:x") is None
        mp.assert_not_called()


class TestListProfileArnCache:
    @pytest.fixture(autouse=True)
    def _clear_arn_cache(self):
        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()
        yield
        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()

    def test_definitive_answer_is_memoized(self):
        # ListAvailableProfiles is called once; the second lookup hits the cache.
        with patch.object(api, "_post",
                          return_value=_resp(200, {"profiles": [{"arn": "arn:x"}]})) as mp:
            assert api._list_profile_arn("tok") == "arn:x"
            assert api._list_profile_arn("tok") == "arn:x"
        assert mp.call_count == 1

    def test_empty_profiles_is_not_cached(self):
        # A 200 with no profiles may be post-login propagation lag on an
        # enterprise account — pinning None would strand it on the text
        # fallback until restart, so each refresh must re-probe.
        with patch.object(api, "_post", return_value=_resp(200, {"profiles": []})) as mp:
            assert api._list_profile_arn("tok") is None
            assert api._list_profile_arn("tok") is None
        assert mp.call_count == 2

    def test_arn_cache_is_keyed_per_token(self):
        # Two tokens for different accounts must each probe their own account —
        # the first token's ARN (or lack of one) must not be reused for the second.
        with patch.object(api, "_post",
                          return_value=_resp(200, {"profiles": [{"arn": "arn:acct-a"}]})):
            assert api._list_profile_arn("token-a") == "arn:acct-a"
        with patch.object(api, "_post",
                          return_value=_resp(200, {"profiles": [{"arn": "arn:acct-b"}]})) as mp:
            assert api._list_profile_arn("token-b") == "arn:acct-b"
        assert mp.call_count == 1  # token-b probed, not served token-a's cache

    def test_transient_failure_is_not_cached(self):
        # A non-200 must NOT pin the cache to None — the next refresh retries.
        with patch.object(api, "_post", return_value=_resp(500, {})):
            assert api._list_profile_arn("tok") is None
        with patch.object(api, "_post",
                          return_value=_resp(200, {"profiles": [{"arn": "arn:y"}]})):
            assert api._list_profile_arn("tok") == "arn:y"

    def test_malformed_profiles_shape_returns_none(self):
        # A non-dict body, non-list profiles, or [null] entries must fail closed
        # (return None) rather than raise and strand the caller.
        with patch.object(api, "_post", return_value=_resp(200, [])):
            assert api._list_profile_arn("t1") is None
        with patch.object(api, "_post", return_value=_resp(200, {"profiles": None})):
            assert api._list_profile_arn("t2") is None
        with patch.object(api, "_post", return_value=_resp(200, {"profiles": [None]})):
            assert api._list_profile_arn("t3") is None


class TestExpectedArnAnchor:
    """A candidate credential is only used when it belongs to the account
    ``kiro-cli whoami`` reports.

    The bug: after switching Kiro profile, the previous profile's token is still
    unexpired and still accepted by GetUsageLimits, so it supplied the credit
    numbers — and a restart did not help, because candidate order was identical
    on boot. Anchoring on whoami's profile ARN is what makes that impossible.
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

    def _fake_post(self, arn_for: dict[str, str], usage_for: dict[str, dict]):
        def fake_post(token, target, payload):
            if target == api._TARGET_LIST_PROFILES:
                arn = arn_for.get(token)
                profiles = [{"arn": arn}] if arn else []
                return _resp(200, {"profiles": profiles})
            return _resp(200, usage_for[token])
        return fake_post

    def test_foreign_credential_is_skipped_for_the_matching_one(self):
        # Old profile's credential is first in the candidate list AND would be
        # accepted by the API; the anchor must reach past it to the one that
        # belongs to the signed-in account.
        usage = {
            "old-profile": {"usageBreakdownList": [
                {"resourceType": "CREDIT", "currentUsage": 49.0, "usageLimit": 50.0}]},
            "new-profile": {"usageBreakdownList": [
                {"resourceType": "CREDIT", "currentUsage": 1200.0, "usageLimit": 10000.0}]},
        }
        arns = {"old-profile": self.ARN_B, "new-profile": self.ARN_A}
        with patch.object(api, "_candidate_tokens",
                          return_value=[_cand("old-profile"), _cand("new-profile")]), \
             patch.object(api, "_post", side_effect=self._fake_post(arns, usage)):
            out = api.fetch_usage_limits(expected_arn=self.ARN_A)
        assert out is not None
        assert out["credits_plan"] == 10000.0, "used the signed-out profile's plan"
        assert out["credits_used"] == 1200.0
        assert out["_profile_arn"] == self.ARN_A

    def test_foreign_credential_is_never_spent_on_a_usage_call(self):
        # The mismatching credential must be dropped at the ARN probe, before a
        # GetUsageLimits request is made with it.
        usage = {"new-profile": {"usageBreakdownList": [
            {"resourceType": "CREDIT", "currentUsage": 1.0, "usageLimit": 10.0}]}}
        arns = {"old-profile": self.ARN_B, "new-profile": self.ARN_A}
        seen: list[tuple[str, str]] = []

        inner = self._fake_post(arns, usage)

        def recording_post(token, target, payload):
            seen.append((token, target))
            return inner(token, target, payload)

        with patch.object(api, "_candidate_tokens",
                          return_value=[_cand("old-profile"), _cand("new-profile")]), \
             patch.object(api, "_post", side_effect=recording_post):
            assert api.fetch_usage_limits(expected_arn=self.ARN_A) is not None
        assert ("old-profile", api._TARGET_GET_USAGE) not in seen

    def test_all_foreign_fails_closed_to_the_text_scrape(self):
        # Nothing matches -> None, so the caller degrades to scraping kiro-cli's
        # own /usage output (right account by construction) rather than showing
        # another account's balance.
        usage = {"old-profile": {"usageBreakdownList": [
            {"resourceType": "CREDIT", "currentUsage": 49.0, "usageLimit": 50.0}]}}
        with patch.object(api, "_candidate_tokens", return_value=[_cand("old-profile")]), \
             patch.object(api, "_post",
                          side_effect=self._fake_post({"old-profile": self.ARN_B}, usage)):
            assert api.fetch_usage_limits(expected_arn=self.ARN_A) is None

    def test_unresolvable_arn_is_not_treated_as_a_match(self):
        # A transient ListAvailableProfiles failure yields arn=None. With an
        # anchor set that is NOT a match — otherwise a probe outage would
        # reopen the exact hole being closed.
        usage = {"tok": {"usageBreakdownList": [
            {"resourceType": "CREDIT", "currentUsage": 1.0, "usageLimit": 10.0}]}}

        def fake_post(token, target, payload):
            if target == api._TARGET_LIST_PROFILES:
                return _resp(500, {})
            return _resp(200, usage[token])

        with patch.object(api, "_candidate_tokens", return_value=[_cand("tok")]), \
             patch.object(api, "_post", side_effect=fake_post):
            assert api.fetch_usage_limits(expected_arn=self.ARN_A) is None

    def test_two_arnless_accounts_are_never_matched_to_each_other(self):
        # Builder ID account B is signed in; a leftover credential from Builder ID
        # account A is still readable. Both probe to arn=None, so no ARN comparison
        # can separate them -- PROVENANCE is what does. A's leftover token lives in
        # a JSON SSO cache or another product's store (kiro-cli rewrites its OWN
        # store on login), so own=False, and it is refused.
        usage = {"leftover-a": {"usageBreakdownList": [
            {"resourceType": "CREDIT", "currentUsage": 49.0, "usageLimit": 50.0}]}}
        with patch.object(api, "_candidate_tokens",
                          return_value=[_cand("leftover-a", own=False)]), \
             patch.object(api, "_post", side_effect=self._fake_post({}, usage)):
            assert api.fetch_usage_limits(expected_arn=None) is None

    def test_arnless_credential_from_the_cli_store_is_accepted(self):
        # The other half of the rule, and why Builder ID accounts keep the FREE API
        # path instead of being pushed onto the credit-spending scrape: a token from
        # kiro-cli's own store IS the signed-in account's credential, so no ARN is
        # needed to trust it.
        usage = {"builder": {"usageBreakdownList": [
            {"resourceType": "CREDIT", "currentUsage": 3.0, "usageLimit": 50.0}]}}
        with patch.object(api, "_candidate_tokens",
                          return_value=[_cand("builder", own=True)]), \
             patch.object(api, "_post", side_effect=self._fake_post({}, usage)):
            out = api.fetch_usage_limits(expected_arn=None)
        assert out is not None
        assert out["credits_used"] == 3.0

    def test_cli_store_credential_wins_over_a_foreign_arnless_one(self):
        # Ordering puts the foreign credential first; provenance must reach past it
        # rather than taking the first thing that is merely readable.
        usage = {
            "leftover-a": {"usageBreakdownList": [
                {"resourceType": "CREDIT", "currentUsage": 49.0, "usageLimit": 50.0}]},
            "builder": {"usageBreakdownList": [
                {"resourceType": "CREDIT", "currentUsage": 3.0, "usageLimit": 50.0}]},
        }
        with patch.object(api, "_candidate_tokens",
                          return_value=[_cand("leftover-a", own=False),
                                        _cand("builder", own=True)]), \
             patch.object(api, "_post", side_effect=self._fake_post({}, usage)):
            out = api.fetch_usage_limits(expected_arn=None)
        assert out is not None
        assert out["credits_used"] == 3.0, "served the foreign account's balance"

    def test_enterprise_arn_is_still_sent_when_whoami_gave_none(self):
        # An org account on a kiro-cli that prints no "Profile:" block: whoami
        # yields no ARN, so this takes the provenance route -- but the credential's
        # OWN ARN must still reach the payload, or the API returns 403 and the
        # account loses its overage figure to the scrape.
        arn = "arn:aws:codewhisperer:us-east-1:9:profile/ORG"
        usage = {"cli": {"usageBreakdownList": [
            {"resourceType": "CREDIT", "currentUsage": 12000.0, "usageLimit": 10000.0}]}}
        sent: list[dict] = []
        inner = self._fake_post({"cli": arn}, usage)

        def recording_post(token, target, payload):
            if target == api._TARGET_GET_USAGE:
                sent.append(payload)
            return inner(token, target, payload)

        with patch.object(api, "_candidate_tokens",
                          return_value=[_cand("cli", own=True)]), \
             patch.object(api, "_post", side_effect=recording_post):
            out = api.fetch_usage_limits(expected_arn=None)
        assert out is not None
        assert out["credits_overage"] == 2000.0
        assert sent and sent[0].get("profileArn") == arn

    def test_arnless_candidate_rejected_against_a_real_anchor(self):
        # ARN-anchored mode: a candidate with no ARN cannot match a real one, and
        # provenance does NOT override an explicit ARN anchor.
        usage = {"leftover-a": {"usageBreakdownList": [
            {"resourceType": "CREDIT", "currentUsage": 49.0, "usageLimit": 50.0}]}}
        with patch.object(api, "_candidate_tokens",
                          return_value=[_cand("leftover-a", own=True)]), \
             patch.object(api, "_post", side_effect=self._fake_post({}, usage)):
            assert api.fetch_usage_limits(expected_arn=self.ARN_A) is None

    def test_foreign_arnless_candidate_is_never_spent_on_a_request(self):
        usage = {"leftover-a": {"usageBreakdownList": [
            {"resourceType": "CREDIT", "currentUsage": 49.0, "usageLimit": 50.0}]}}
        seen: list[tuple[str, str]] = []
        inner = self._fake_post({}, usage)

        def recording_post(token, target, payload):
            seen.append((token, target))
            return inner(token, target, payload)

        with patch.object(api, "_candidate_tokens",
                          return_value=[_cand("leftover-a", own=False)]), \
             patch.object(api, "_post", side_effect=recording_post):
            assert api.fetch_usage_limits(expected_arn=None) is None
        # Refused before ANY request -- not even the profile probe is spent on it.
        assert seen == []

    def test_empty_anchor_behaves_as_no_anchor(self):
        # A falsy ARN means "nothing to match on", so it takes the provenance route
        # rather than being compared as a literal.
        usage = {"tok": {"usageBreakdownList": [
            {"resourceType": "CREDIT", "currentUsage": 1.0, "usageLimit": 10.0}]}}
        with patch.object(api, "_candidate_tokens",
                          return_value=[_cand("tok", own=False)]), \
             patch.object(api, "_post", side_effect=self._fake_post({}, usage)):
            assert api.fetch_usage_limits(expected_arn="") is None


class TestCandidateOrdering:
    """Candidates are ranked by expiry (freshest first), not by path order."""

    def test_freshest_expiry_ranks_first(self, tmp_path):
        # A stale-but-unexpired JSON credential sits in the highest-priority PATH
        # slot; the SQLite store holds a newer one. Path order used to decide,
        # which is how a signed-out profile won.
        now = datetime.now(timezone.utc)
        soon = (now + timedelta(minutes=20)).isoformat()
        later = (now + timedelta(hours=8)).isoformat()
        stale = json.dumps({"accessToken": "older-json", "expiresAt": soon}).encode()

        db = tmp_path / "data.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        con.execute(
            "INSERT INTO auth_kv VALUES (?, ?)",
            ("kirocli:odic:token",
             json.dumps({"access_token": "newer-sqlite", "expires_at": later})),
        )
        con.commit()
        con.close()

        def fake_read(read_id):
            return stale if read_id.endswith("cli") else None

        with patch("kiro_crew.hooks.safe_read_file_internal", side_effect=fake_read), \
             patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True), \
             patch.object(api, "_CLI_SQLITE_DBS", (db,)), \
             patch.object(api, "_OTHER_SQLITE_DBS", ()):
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

        with patch("kiro_crew.hooks.safe_read_file_internal", side_effect=fake_read), \
             patch.object(api, "_CLI_SQLITE_DBS", ()), \
             patch.object(api, "_OTHER_SQLITE_DBS", ()):
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

        with patch("kiro_crew.hooks.safe_read_file_internal", side_effect=fake_read), \
             patch.object(api, "_CLI_SQLITE_DBS", ()), \
             patch.object(api, "_OTHER_SQLITE_DBS", ()):
            cands = api._candidate_tokens()
        assert [c.token for c in cands] == ["same"]

    def test_expired_candidates_excluded(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        blob = json.dumps({"accessToken": "old", "expiresAt": past}).encode()
        with patch("kiro_crew.hooks.safe_read_file_internal", return_value=blob), \
             patch.object(api, "_CLI_SQLITE_DBS", ()), \
             patch.object(api, "_OTHER_SQLITE_DBS", ()):
            assert api._candidate_tokens() == []


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
            with patch.object(api, "_post",
                              return_value=_resp(200, {"profiles": [{"arn": f"arn:{i}"}]})):
                api._list_profile_arn(f"token-{i}")
        assert len(api._PROFILE_ARN_CACHE) <= api._PROFILE_CACHE_MAX
        assert len(api._PROFILE_NAME_CACHE) <= api._PROFILE_CACHE_MAX

    def test_both_caches_evict_together(self):
        # A display name must never outlive the ARN it belongs to, or it could be
        # shown next to a different account's credits.
        for i in range(api._PROFILE_CACHE_MAX + 5):
            with patch.object(api, "_post", return_value=_resp(200, {"profiles": [
                    {"arn": f"arn:{i}", "profileName": f"Org {i}"}]})):
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
        for base in (".local/share", "Library/Application Support"):
            for app in ("kiro-cli", "amazon-q"):
                # The DB and its WAL/SHM/journal sidecars must all be sensitive.
                assert is_sensitive_path(str(home / base / app / "data.sqlite3"))
                assert is_sensitive_path(str(home / base / app / "data.sqlite3-wal"))
