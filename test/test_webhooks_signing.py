"""HMAC request signing for ``POST /api/hooks/agent``.

The bearer token proves WHO is calling; the signature proves the body arrived
unmodified and is not a replay. These tests pin both halves of that contract:

* the store side — a per-token signing secret is generated at mint, persisted
  (an HMAC verifier has to be able to recompute it) and never echoed by any
  read path;
* the handler side — verification order, one distinct 401 error per cause, the
  raw-bytes signing payload, replay refusal inside the window, and signature
  failures feeding the same per-source throttle as a bad bearer.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import webhooks
from kiro_crew.dashboard.handlers import hooks as H


@pytest.fixture(autouse=True)
def _isolate_process_globals():
    """Reset the replay set and throttle around EVERY test in this module.

    ``_seen_signatures`` and the auth-throttle buckets are process-global. The
    helper tests below deliberately reuse one secret, timestamp and body, so
    several of them compute the SAME signature — meaning whichever runs second
    sees its own digest as a replay unless the set is cleared between tests.
    Resetting only inside ``wired`` left that ordering dependency live for the
    tests that do not take the fixture, which surfaced as a shard-order-dependent
    failure in CI (``assert 'signature already used' is None``) rather than
    locally.
    """
    webhooks._reset_auth_throttle()
    webhooks._reset_signature_replay()
    H._reset_hook_inflight()
    yield
    webhooks._reset_auth_throttle()
    webhooks._reset_signature_replay()
    H._reset_hook_inflight()


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """Point the webhook stores and hooks.json at a temp dir, reset globals."""
    monkeypatch.setattr(webhooks, "config_dir", lambda: Path(tmp_path))
    monkeypatch.setattr(H, "_HOOK_STORE_PATH", Path(tmp_path) / "hooks.json")
    monkeypatch.setattr(H, "_sel", lambda: MagicMock())
    monkeypatch.setattr(H, "_legacy_hook_token", lambda: "")
    webhooks._reset_auth_throttle()
    webhooks._reset_signature_replay()
    yield Path(tmp_path)
    webhooks._reset_auth_throttle()
    webhooks._reset_signature_replay()


def _hook_request(
    body: dict,
    *,
    bearer: str,
    secret: str | None = None,
    timestamp: int | None = None,
    signature: str | None = None,
    sign_body: bytes | None = None,
    extra_headers: dict | None = None,
):
    """A mocked ``POST /api/hooks/agent`` whose raw body is byte-accurate.

    *sign_body* signs bytes OTHER than the ones transmitted, which is how the
    tampered-body case is expressed. *signature* overrides the computed header
    outright for the malformed cases.
    """
    raw = json.dumps(body).encode("utf-8")
    headers = {"Authorization": f"Bearer {bearer}"}
    if secret is not None:
        stamp = int(time.time()) if timestamp is None else timestamp
        headers[webhooks.TIMESTAMP_HEADER] = str(stamp)
        headers[webhooks.SIGNATURE_HEADER] = webhooks.sign_payload(
            secret, stamp, sign_body if sign_body is not None else raw
        )
    if signature is not None:
        headers[webhooks.SIGNATURE_HEADER] = signature
    headers.update(extra_headers or {})
    req = make_mocked_request("POST", "/api/hooks/agent", headers=headers)
    req.app["state"] = MagicMock()
    req.app.get = lambda key, default=None: {"port": 6776}.get(key, default)
    req.read = AsyncMock(return_value=raw)
    # Deliberately wired to a DIFFERENT body: any handler that parses via
    # request.json() instead of the raw bytes it verified will show up here.
    req.json = AsyncMock(return_value={"message": "PARSED-FROM-JSON-NOT-RAW"})
    return req


async def _payload(resp):
    return json.loads(resp.body.decode("utf-8"))


PROBE_BODY = {"message": "hello", "sessionKey": "hook:review:pr-1", "name": "Bot"}


async def _call(req):
    """Run the handler with the agent turn stubbed out, then free the slot."""
    with patch.object(H, "_run_hook_agent", new=AsyncMock(return_value=None)):
        resp = await H.api_hooks_agent(req)
        if resp.status == 200:
            # Let the fire-and-forget task run, then release the slot the stub
            # never reaches the finally-block to release.
            await asyncio.sleep(0)
            H._hook_semaphore.release()
    return resp


# ── Store ──


class TestSigningSecretStorage:
    def test_generated_at_mint_and_returned_once(self, tmp_path):
        store = webhooks.WebhookTokenStore(tmp_path)
        raw, secret, entry = store.create("Review Bot")
        assert secret.startswith(webhooks.SIGNING_SECRET_PREFIX)
        body = secret[len(webhooks.SIGNING_SECRET_PREFIX):]
        assert len(body) == webhooks.SIGNING_SECRET_ENTROPY_CHARS
        assert entry["require_signature"] is True
        # The create response's own entry must not carry it either — the secret
        # travels as its own top-level field, once.
        assert "signing_secret" not in entry
        assert raw != secret

    def test_secret_is_retrievable_for_the_verifier(self, tmp_path):
        """Unlike the bearer hash, the signing secret must be recomputable."""
        store = webhooks.WebhookTokenStore(tmp_path)
        _raw, secret, entry = store.create("Review Bot")
        stored = store.entry_for(entry["id"])
        assert stored is not None
        assert stored["signing_secret"] == secret
        assert secret in store.path.read_text(encoding="utf-8")
        assert store.entry_for("wht_missing") is None
        assert store.entry_for("") is None

    def test_public_entries_strip_the_secret(self, tmp_path):
        store = webhooks.WebhookTokenStore(tmp_path)
        _raw, secret, _entry = store.create("Review Bot")
        public = store.public_entries()
        assert len(public) == 1
        assert "signing_secret" not in public[0]
        assert "token_hash" not in public[0]
        assert secret not in json.dumps(public)
        assert public[0]["require_signature"] is True

    def test_bearer_only_token_has_no_secret(self, tmp_path):
        store = webhooks.WebhookTokenStore(tmp_path)
        raw, secret, entry = store.create("Legacy CI", require_signature=False)
        assert secret == ""
        assert entry["require_signature"] is False
        stored = store.entry_for(entry["id"])
        assert stored is not None and stored["signing_secret"] == ""
        assert store.public_entries()[0]["require_signature"] is False
        assert store.verify(raw) == entry["id"]

    def test_legacy_config_entry_is_bearer_only(self, tmp_path):
        store = webhooks.WebhookTokenStore(tmp_path)
        legacy = store.public_entries(legacy_token="cfg-secret")[0]
        assert legacy["id"] == webhooks.LEGACY_TOKEN_ID
        assert legacy["require_signature"] is False


# ── verify_signature ──


class TestVerifySignatureHelper:
    SECRET = "kc_whs_unit-test-secret"

    def test_accepts_a_correct_signature(self):
        now = 1_800_000_000.0
        body = b'{"message":"hi"}'
        sig = webhooks.sign_payload(self.SECRET, int(now), body)
        assert sig.startswith("sha256=")
        assert webhooks.verify_signature(
            secret=self.SECRET, timestamp=str(int(now)), signature=sig, body=body, now=now
        ) is None

    def test_signed_payload_is_timestamp_dot_raw_body(self):
        """Pin the exact signed string so the docs and callers cannot drift."""
        import hashlib
        import hmac as _hmac

        body = b'{"a": 1}'
        expected = _hmac.new(
            self.SECRET.encode(), b"1800000000." + body, hashlib.sha256
        ).hexdigest()
        assert webhooks.sign_payload(self.SECRET, 1800000000, body) == f"sha256={expected}"

    def test_missing_headers(self):
        body = b"{}"
        for stamp, sig in ((None, "sha256=aa"), ("1800000000", None), (None, None)):
            assert webhooks.verify_signature(
                secret=self.SECRET, timestamp=stamp, signature=sig, body=body,
                now=1_800_000_000.0,
            ) == webhooks.SIG_ERR_MISSING

    def test_unparseable_timestamp(self):
        assert webhooks.verify_signature(
            secret=self.SECRET, timestamp="not-a-number", signature="sha256=aa",
            body=b"{}", now=1_800_000_000.0,
        ) == webhooks.SIG_ERR_TIMESTAMP

    def test_timestamp_window_boundaries(self):
        now = 1_800_000_000.0
        body = b"{}"

        def _check(offset: int) -> str | None:
            stamp = int(now) + offset
            return webhooks.verify_signature(
                secret=self.SECRET,
                timestamp=str(stamp),
                signature=webhooks.sign_payload(self.SECRET, stamp, body),
                body=body,
                now=now,
            )

        window = webhooks.SIGNATURE_WINDOW_SECONDS
        assert window == 300
        assert _check(window) is None          # exactly +300s accepted
        webhooks._reset_signature_replay()
        assert _check(-window) is None         # exactly -300s accepted
        assert _check(window + 1) == webhooks.SIG_ERR_WINDOW
        assert _check(-(window + 1)) == webhooks.SIG_ERR_WINDOW

    def test_malformed_signature_header(self):
        now = 1_800_000_000.0
        for bad in ("deadbeef", "sha256=", "sha512=abcd", "sha256=zzzz", "sha1=aa"):
            assert webhooks.verify_signature(
                secret=self.SECRET, timestamp=str(int(now)), signature=bad,
                body=b"{}", now=now,
            ) == webhooks.SIG_ERR_MALFORMED

    def test_wrong_secret_or_body_mismatches(self):
        now = 1_800_000_000.0
        body = b'{"message":"hi"}'
        good = webhooks.sign_payload(self.SECRET, int(now), body)
        assert webhooks.verify_signature(
            secret="kc_whs_other", timestamp=str(int(now)), signature=good, body=body,
            now=now,
        ) == webhooks.SIG_ERR_MISMATCH
        assert webhooks.verify_signature(
            secret=self.SECRET, timestamp=str(int(now)), signature=good,
            body=b'{"message":"hi "}', now=now,
        ) == webhooks.SIG_ERR_MISMATCH

    def test_uppercase_hex_still_verifies(self):
        now = 1_800_000_000.0
        sig = webhooks.sign_payload(self.SECRET, int(now), b"{}").upper()
        assert webhooks.verify_signature(
            secret=self.SECRET, timestamp=str(int(now)),
            signature="sha256=" + sig.split("=")[1], body=b"{}", now=now,
        ) is None

    def test_absent_secret_fails_closed(self):
        now = 1_800_000_000.0
        assert webhooks.verify_signature(
            secret="", timestamp=str(int(now)),
            signature=webhooks.sign_payload("", int(now), b"{}"), body=b"{}", now=now,
        ) == webhooks.SIG_ERR_NO_SECRET

    def test_replay_of_identical_signature_refused(self):
        now = 1_800_000_000.0
        body = b'{"message":"hi"}'
        sig = webhooks.sign_payload(self.SECRET, int(now), body)
        kwargs = dict(secret=self.SECRET, timestamp=str(int(now)), signature=sig, body=body)
        assert webhooks.verify_signature(now=now, **kwargs) is None
        assert webhooks.verify_signature(now=now + 1, **kwargs) == webhooks.SIG_ERR_REPLAY
        # Still refused right up to the edge of the window.
        assert webhooks.verify_signature(
            now=now + webhooks.SIGNATURE_WINDOW_SECONDS, **kwargs
        ) == webhooks.SIG_ERR_REPLAY

    def test_failed_signatures_never_enter_the_replay_set(self):
        """A bad-digest flood must not be able to evict live entries."""
        now = 1_800_000_000.0
        # The seen-set is module-global and other cases in this file populate it,
        # so reset first: this test is about what a FAILED verify adds (nothing),
        # not about the set being empty by coincidence of test order.
        webhooks._reset_signature_replay()
        webhooks.verify_signature(
            secret=self.SECRET, timestamp=str(int(now)),
            signature="sha256=" + "ab" * 32, body=b"{}", now=now,
        )
        assert webhooks._seen_signatures == {}

    def test_seen_set_is_bounded_by_refusing_not_evicting(self):
        """At the cap the set stops accepting; it must not drop a live entry.

        Evicting the oldest still-valid signature would forget one the window
        check would still accept, so the captured request behind it would be
        admitted a second time — replay protection quietly failing open under
        load. Refusing the extra call is the lesser failure.
        """
        now = 1_800_000_000.0
        first_body = b'{"n":0}'
        first_sig = webhooks.sign_payload(self.SECRET, int(now), first_body)
        results = []
        for i in range(webhooks.SIGNATURE_SEEN_MAX + 100):
            body = f'{{"n":{i}}}'.encode()
            results.append(webhooks.verify_signature(
                secret=self.SECRET,
                timestamp=str(int(now)),
                signature=webhooks.sign_payload(self.SECRET, int(now), body),
                body=body,
                now=now,
            ))

        assert len(webhooks._seen_signatures) <= webhooks.SIGNATURE_SEEN_MAX
        # Behavioural assertion FIRST, so this test discriminates on conduct
        # rather than on the existence of a new constant: the very first
        # signature must still be remembered, i.e. replaying it is refused. Under
        # oldest-first eviction it has been forgotten and is accepted again.
        assert webhooks.verify_signature(
            secret=self.SECRET, timestamp=str(int(now)), signature=first_sig,
            body=first_body, now=now,
        ) == webhooks.SIG_ERR_REPLAY
        # The overflow calls were refused, with the cause named distinctly so a
        # caller can tell "you replayed" from "we are saturated, retry".
        assert results[:webhooks.SIGNATURE_SEEN_MAX] == [None] * webhooks.SIGNATURE_SEEN_MAX
        assert set(results[webhooks.SIGNATURE_SEEN_MAX:]) == {webhooks.SIG_ERR_REPLAY_CAPACITY}

    def test_an_absurdly_long_timestamp_is_rejected_not_a_500(self):
        """Python ints are arbitrary precision; float comparison is not.

        A 309-digit timestamp parsed fine and then raised OverflowError on the
        window comparison, so an authenticated caller's bad header became an
        HTTP 500 instead of a 401.
        """
        body = b'{}'
        for raw in ('9' * 309, '-' + '9' * 400, '1' * 21):
            assert webhooks.verify_signature(
                secret=self.SECRET,
                timestamp=raw,
                signature=webhooks.sign_payload(self.SECRET, raw, body),
                body=body,
                now=1_800_000_000.0,
            ) == webhooks.SIG_ERR_TIMESTAMP

    def test_seen_set_ages_out(self):
        now = 1_800_000_000.0
        body = b"{}"
        sig = webhooks.sign_payload(self.SECRET, int(now), body)
        assert webhooks.verify_signature(
            secret=self.SECRET, timestamp=str(int(now)), signature=sig, body=body, now=now
        ) is None
        # Past the window the entry is swept; the timestamp check is what makes
        # a genuine replay impossible at that point, not the seen-set.
        later = now + webhooks.SIGNATURE_WINDOW_SECONDS + 1
        webhooks.verify_signature(
            secret=self.SECRET, timestamp=str(int(later)),
            signature=webhooks.sign_payload(self.SECRET, int(later), body),
            body=body, now=later,
        )
        assert sig[len(webhooks.SIGNATURE_SCHEME):] not in webhooks._seen_signatures


# ── Handler ──


class TestSignedRequests:
    @pytest.mark.asyncio
    async def test_valid_signature_accepted(self, wired):
        raw, secret, entry = webhooks.token_store().create("Review Bot")
        assert webhooks.token_store().entry_for(entry["id"])["last_used_at"] is None
        resp = await _call(_hook_request(PROBE_BODY, bearer=raw, secret=secret))
        assert resp.status == 200
        assert (await _payload(resp))["status"] == "accepted"
        assert webhooks.token_store().entry_for(entry["id"])["last_used_at"] is not None
        assert webhooks.run_store().list_runs() == []

    @pytest.mark.asyncio
    async def test_failed_signature_does_not_stamp_last_used(self, wired):
        raw, secret, entry = webhooks.token_store().create("Review Bot")
        req = _hook_request(
            PROBE_BODY,
            bearer=raw,
            secret=secret,
            sign_body=b'{"message":"tampered"}',
        )
        resp = await _call(req)
        assert resp.status == 401
        assert (await _payload(resp))["error"] == webhooks.SIG_ERR_MISMATCH
        assert webhooks.token_store().entry_for(entry["id"])["last_used_at"] is None

    @pytest.mark.asyncio
    async def test_body_is_parsed_from_the_raw_bytes(self, wired):
        """The verified bytes and the executed payload must be the same bytes."""
        raw, secret, _entry = webhooks.token_store().create("Review Bot")
        req = _hook_request(PROBE_BODY, bearer=raw, secret=secret)
        with patch.object(H, "_run_hook_agent", new=AsyncMock(return_value=None)) as run:
            resp = await H.api_hooks_agent(req)
            await asyncio.sleep(0)
            H._hook_semaphore.release()
        assert resp.status == 200
        # Positional args: (state, session_key, message, name, ...)
        assert run.call_args.args[1] == PROBE_BODY["sessionKey"]
        assert run.call_args.args[2] == PROBE_BODY["message"]
        req.json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_signature_headers_rejected(self, wired):
        raw, _secret, _entry = webhooks.token_store().create("Review Bot")
        resp = await _call(_hook_request(PROBE_BODY, bearer=raw))
        assert resp.status == 401
        assert (await _payload(resp))["error"] == webhooks.SIG_ERR_MISSING

    @pytest.mark.asyncio
    async def test_timestamp_only_is_still_missing(self, wired):
        raw, _secret, _entry = webhooks.token_store().create("Review Bot")
        resp = await _call(
            _hook_request(
                PROBE_BODY,
                bearer=raw,
                extra_headers={webhooks.TIMESTAMP_HEADER: str(int(time.time()))},
            )
        )
        assert resp.status == 401
        assert (await _payload(resp))["error"] == webhooks.SIG_ERR_MISSING

    @pytest.mark.asyncio
    async def test_timestamp_outside_window_rejected(self, wired):
        raw, secret, _entry = webhooks.token_store().create("Review Bot")
        stale = int(time.time()) - (webhooks.SIGNATURE_WINDOW_SECONDS + 60)
        resp = await _call(
            _hook_request(PROBE_BODY, bearer=raw, secret=secret, timestamp=stale)
        )
        assert resp.status == 401
        assert (await _payload(resp))["error"] == webhooks.SIG_ERR_WINDOW

        ahead = int(time.time()) + (webhooks.SIGNATURE_WINDOW_SECONDS + 60)
        resp = await _call(
            _hook_request(PROBE_BODY, bearer=raw, secret=secret, timestamp=ahead)
        )
        assert (await _payload(resp))["error"] == webhooks.SIG_ERR_WINDOW

    @pytest.mark.asyncio
    async def test_non_integer_timestamp_rejected(self, wired):
        raw, secret, _entry = webhooks.token_store().create("Review Bot")
        req = _hook_request(
            PROBE_BODY, bearer=raw, secret=secret,
            extra_headers={webhooks.TIMESTAMP_HEADER: "yesterday"},
        )
        resp = await _call(req)
        assert resp.status == 401
        assert (await _payload(resp))["error"] == webhooks.SIG_ERR_TIMESTAMP

    @pytest.mark.asyncio
    async def test_tampered_body_rejected(self, wired):
        """Signature computed over the original body; a different body is sent."""
        raw, secret, _entry = webhooks.token_store().create("Review Bot")
        req = _hook_request(
            {"message": "rm -rf everything", "sessionKey": "hook:review:pr-1"},
            bearer=raw,
            secret=secret,
            sign_body=json.dumps(PROBE_BODY).encode("utf-8"),
        )
        resp = await _call(req)
        assert resp.status == 401
        assert (await _payload(resp))["error"] == webhooks.SIG_ERR_MISMATCH

    @pytest.mark.asyncio
    async def test_malformed_signature_rejected(self, wired):
        raw, secret, _entry = webhooks.token_store().create("Review Bot")
        req = _hook_request(
            PROBE_BODY, bearer=raw, secret=secret, signature="not-a-scheme"
        )
        resp = await _call(req)
        assert resp.status == 401
        assert (await _payload(resp))["error"] == webhooks.SIG_ERR_MALFORMED

    @pytest.mark.asyncio
    async def test_replayed_request_rejected(self, wired):
        """Byte-identical replay inside the window is refused the second time."""
        raw, secret, _entry = webhooks.token_store().create("Review Bot")
        stamp = int(time.time())
        first = _hook_request(PROBE_BODY, bearer=raw, secret=secret, timestamp=stamp)
        replay = _hook_request(PROBE_BODY, bearer=raw, secret=secret, timestamp=stamp)
        assert first.headers[webhooks.SIGNATURE_HEADER] == (
            replay.headers[webhooks.SIGNATURE_HEADER]
        )
        assert (await _call(first)).status == 200
        resp = await _call(replay)
        assert resp.status == 401
        assert (await _payload(resp))["error"] == webhooks.SIG_ERR_REPLAY

    @pytest.mark.asyncio
    async def test_secret_missing_for_signing_token_fails_closed(self, wired):
        """A hand-edited store must not silently downgrade to bearer-only."""
        store = webhooks.token_store()
        raw, _secret, entry = store.create("Review Bot")
        data = json.loads(store.path.read_text(encoding="utf-8"))
        data["tokens"][0]["signing_secret"] = ""
        store.path.write_text(json.dumps(data), encoding="utf-8")
        assert store.entry_for(entry["id"])["require_signature"] is True

        resp = await _call(_hook_request(PROBE_BODY, bearer=raw, secret="kc_whs_guess"))
        assert resp.status == 401
        assert (await _payload(resp))["error"] == webhooks.SIG_ERR_NO_SECRET


class TestBearerOnlyTokensUnaffected:
    @pytest.mark.asyncio
    async def test_token_minted_without_signing_needs_no_headers(self, wired):
        raw, secret, entry = webhooks.token_store().create("CI runner", False)
        assert secret == ""
        assert entry["require_signature"] is False
        resp = await _call(_hook_request(PROBE_BODY, bearer=raw))
        assert resp.status == 200
        assert (await _payload(resp))["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_bearer_only_token_ignores_junk_signature_headers(self, wired):
        """Signing is per token — an unused header must not start being enforced."""
        raw, _secret, _entry = webhooks.token_store().create("CI runner", False)
        resp = await _call(
            _hook_request(
                PROBE_BODY,
                bearer=raw,
                extra_headers={
                    webhooks.TIMESTAMP_HEADER: "0",
                    webhooks.SIGNATURE_HEADER: "sha256=" + "00" * 32,
                },
            )
        )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_legacy_config_token_stays_bearer_only(self, wired, monkeypatch):
        """Existing installs on hooks.webhook_token must not break on upgrade."""
        monkeypatch.setattr(H, "_legacy_hook_token", lambda: "cfg-secret")
        resp = await _call(_hook_request(PROBE_BODY, bearer="cfg-secret"))
        assert resp.status == 200
        assert (await _payload(resp))["status"] == "accepted"


class TestSignatureFailuresAreRecordedAndThrottled:
    @pytest.mark.asyncio
    async def test_unauthorized_run_names_the_cause(self, wired):
        raw, secret, entry = webhooks.token_store().create("Review Bot")
        stale = int(time.time()) - 10_000
        await _call(_hook_request(PROBE_BODY, bearer=raw, secret=secret, timestamp=stale))
        runs = webhooks.run_store().list_runs()
        assert len(runs) == 1
        assert runs[0]["outcome"] == webhooks.OUTCOME_UNAUTHORIZED
        assert webhooks.SIG_ERR_WINDOW in runs[0]["detail"]
        # The bearer identified the caller even though the signature did not
        # verify, so the run points at the token that failed to sign.
        assert runs[0]["token_id"] == entry["id"]
        assert runs[0]["hook_id"] is None

    @pytest.mark.asyncio
    async def test_signature_failure_trips_the_same_throttle_as_a_bad_bearer(self, wired):
        raw, secret, _entry = webhooks.token_store().create("Review Bot")

        def _bad():
            return _hook_request(
                PROBE_BODY, bearer=raw, secret=secret,
                sign_body=b'{"tampered":true}',
            )

        for _ in range(webhooks._AUTH_FAIL_LIMIT - 1):
            resp = await _call(_bad())
            assert resp.status == 401
        # The failure that reaches the limit is still answered as a 401 ...
        assert (await _call(_bad())).status == 401
        # ... and the next call from that source is refused before any auth work.
        resp = await _call(_bad())
        assert resp.status == 429
        assert (await _payload(resp))["error"] == "too many failed attempts"

    @pytest.mark.asyncio
    async def test_a_valid_signed_call_clears_the_failure_counter(self, wired):
        raw, secret, _entry = webhooks.token_store().create("Review Bot")
        for _ in range(webhooks._AUTH_FAIL_LIMIT - 1):
            await _call(
                _hook_request(
                    PROBE_BODY, bearer=raw, secret=secret, sign_body=b'{"tampered":true}'
                )
            )
        assert (await _call(_hook_request(PROBE_BODY, bearer=raw, secret=secret))).status == 200
        # Counter cleared, so the next bad signature is the first of a new window.
        for _ in range(webhooks._AUTH_FAIL_LIMIT - 1):
            resp = await _call(
                _hook_request(
                    PROBE_BODY, bearer=raw, secret=secret, sign_body=b'{"tampered":true}'
                )
            )
            assert resp.status == 401


class TestReadEndpointNeverLeaksTheSecret:
    @pytest.mark.asyncio
    async def test_get_webhooks_omits_signing_secret(self, wired, monkeypatch):
        monkeypatch.setattr(H, "_legacy_hook_token", lambda: "cfg-secret")
        _raw, secret, entry = webhooks.token_store().create("Review Bot")
        req = make_mocked_request("GET", "/api/webhooks")
        req.app["state"] = MagicMock()
        req.app.get = lambda key, default=None: {"port": 6776}.get(key, default)
        data = json.loads((await H.api_webhooks(req)).body.decode("utf-8"))

        blob = json.dumps(data)
        assert secret not in blob
        assert "signing_secret" not in blob
        by_id = {t["id"]: t for t in data["tokens"]}
        assert by_id[entry["id"]]["require_signature"] is True
        assert by_id[webhooks.LEGACY_TOKEN_ID]["require_signature"] is False
        assert data["limits"]["signature_window_seconds"] == (
            webhooks.SIGNATURE_WINDOW_SECONDS
        )

    @pytest.mark.asyncio
    async def test_create_returns_the_secret_once_next_to_the_token(self, wired):
        req = make_mocked_request("POST", "/api/webhooks/tokens")
        req.app["state"] = MagicMock()
        req.json = AsyncMock(return_value={"label": "Review Bot"})
        resp = await H.api_webhook_token_create(req)
        assert resp.status == 201
        data = json.loads(resp.body.decode("utf-8"))
        assert data["signing_secret"].startswith(webhooks.SIGNING_SECRET_PREFIX)
        assert data["entry"]["require_signature"] is True
        assert "signing_secret" not in data["entry"]

        # And never again on any read.
        get = make_mocked_request("GET", "/api/webhooks")
        get.app["state"] = MagicMock()
        get.app.get = lambda key, default=None: {"port": 6776}.get(key, default)
        listed = (await H.api_webhooks(get)).body.decode("utf-8")
        assert data["signing_secret"] not in listed

    @pytest.mark.asyncio
    async def test_create_can_opt_out_of_signing(self, wired):
        req = make_mocked_request("POST", "/api/webhooks/tokens")
        req.app["state"] = MagicMock()
        req.json = AsyncMock(
            return_value={"label": "CI runner", "require_signature": False}
        )
        resp = await H.api_webhook_token_create(req)
        assert resp.status == 201
        data = json.loads(resp.body.decode("utf-8"))
        assert "signing_secret" not in data
        assert data["entry"]["require_signature"] is False

    @pytest.mark.asyncio
    async def test_create_rejects_non_boolean_require_signature(self, wired):
        req = make_mocked_request("POST", "/api/webhooks/tokens")
        req.app["state"] = MagicMock()
        req.json = AsyncMock(
            return_value={"label": "CI runner", "require_signature": "false"}
        )
        resp = await H.api_webhook_token_create(req)
        assert resp.status == 400
        assert "boolean" in json.loads(resp.body.decode("utf-8"))["error"]
        assert webhooks.token_store().count() == 0


class TestReplaySetIsThreadSafe:
    """Only ONE of N identical signed requests may be accepted.

    Verification runs in ``asyncio.to_thread`` workers because the endpoint must
    not block the loop, so ``_remember_signature`` is a check-then-act shared by
    several threads. Two identical signed requests arriving together could both
    pass the membership test before either inserted, and both would start an
    agent turn — the exact duplicate the replay set exists to prevent.
    """

    SECRET = "kc_whs_concurrency-test"

    def test_a_future_dated_signature_cannot_be_replayed_while_still_valid(self):
        """The entry must outlive the window that would still accept it.

        The window is symmetric, so a timestamp of T+300 accepted at T stays
        acceptable until T+600. Evicting on INSERTION age dropped that entry at
        T+300, and the replay was then admitted at T+301 with its timestamp still
        inside the window — the exact duplicate turn the set exists to refuse.
        """
        issued_at = 1_800_100_000.0
        future_ts = int(issued_at + webhooks.SIGNATURE_WINDOW_SECONDS)
        body = b'{"message":"future"}'
        sig = webhooks.sign_payload(self.SECRET, future_ts, body)
        kwargs = dict(
            secret=self.SECRET, timestamp=str(future_ts), signature=sig, body=body
        )

        # Accepted now, with a timestamp the window still permits.
        assert webhooks.verify_signature(now=issued_at, **kwargs) is None

        # One second past the OLD eviction horizon, and well inside the window
        # for this timestamp — must still be refused as a replay.
        later = issued_at + webhooks.SIGNATURE_WINDOW_SECONDS + 1
        assert abs(later - future_ts) <= webhooks.SIGNATURE_WINDOW_SECONDS, (
            "premise: the signature's own timestamp is still inside the window"
        )
        assert webhooks.verify_signature(now=later, **kwargs) == webhooks.SIG_ERR_REPLAY

    def test_an_entry_is_dropped_once_its_own_timestamp_leaves_the_window(self):
        """Retention must not become unbounded: past the window it may go.

        Eviction is lazy — it runs inside ``_remember_signature``, so a stale
        entry is swept by the next ACCEPTED verification rather than by the
        rejected one, which returns at the window check before getting there.
        Asserting on the rejected call would be asserting the wrong mechanism.
        """
        now = 1_800_200_000.0
        body = b'{"message":"expiring"}'
        sig = webhooks.sign_payload(self.SECRET, int(now), body)
        digest = sig[len(webhooks.SIGNATURE_SCHEME):]

        assert webhooks.verify_signature(
            secret=self.SECRET, timestamp=str(int(now)), signature=sig,
            body=body, now=now,
        ) is None
        assert digest in webhooks._seen_signatures

        # Beyond the window the timestamp is refused on its own merits, so
        # nothing can be re-admitted even while the entry is still held.
        assert webhooks.verify_signature(
            secret=self.SECRET, timestamp=str(int(now)), signature=sig,
            body=body, now=now + webhooks.SIGNATURE_WINDOW_SECONDS + 5,
        ) == webhooks.SIG_ERR_WINDOW

        # The next accepted call sweeps it, so retention stays bounded.
        later = now + webhooks.SIGNATURE_WINDOW_SECONDS + 5
        other = b'{"message":"other"}'
        other_sig = webhooks.sign_payload(self.SECRET, int(later), other)
        assert webhooks.verify_signature(
            secret=self.SECRET, timestamp=str(int(later)), signature=other_sig,
            body=other, now=later,
        ) is None
        assert digest not in webhooks._seen_signatures

    def test_eviction_check_and_insertion_all_happen_under_one_lock(self):
        """Assert the invariant directly, not by trying to win a race.

        A thread-collision test is not usable here: the membership test and the
        insertion are a handful of bytecodes apart, so the GIL hides the window
        and the test passes with the lock removed — it would assert nothing. This
        instead observes that every read AND write of the set happens while the
        lock is held, which is the property that makes the race impossible.
        """
        held: list[bool] = [False]
        events: list[str] = []
        real_lock = webhooks._seen_signatures_lock

        class _WatchedLock:
            def __enter__(self):
                real_lock.acquire()
                held[0] = True
                return self

            def __exit__(self, *exc):
                held[0] = False
                real_lock.release()
                return False

        class _WatchedDict(dict):
            def __contains__(self, key):
                events.append(f"read:{held[0]}")
                return super().__contains__(key)

            def __setitem__(self, key, value):
                events.append(f"write:{held[0]}")
                super().__setitem__(key, value)

        now = 1_800_000_500.0
        body = b'{"message":"once"}'
        sig = webhooks.sign_payload(self.SECRET, int(now), body)
        kwargs = dict(
            secret=self.SECRET, timestamp=str(int(now)), signature=sig, body=body
        )

        with patch.object(webhooks, "_seen_signatures_lock", _WatchedLock()), \
                patch.object(webhooks, "_seen_signatures", _WatchedDict()):
            assert webhooks.verify_signature(now=now, **kwargs) is None
            # And the second attempt is refused, still under the lock.
            assert webhooks.verify_signature(now=now, **kwargs) == webhooks.SIG_ERR_REPLAY

        assert events, "the replay set was neither read nor written"
        unguarded = [e for e in events if e.endswith(":False")]
        assert not unguarded, f"replay set touched outside the lock: {unguarded}"

    def test_distinct_signatures_all_still_pass_concurrently(self):
        """The lock must not make concurrent DIFFERENT requests reject."""
        from concurrent.futures import ThreadPoolExecutor

        now = 1_800_000_600.0

        def _attempt(i: int):
            body = f'{{"n":{i}}}'.encode()
            sig = webhooks.sign_payload(self.SECRET, int(now), body)
            return webhooks.verify_signature(
                secret=self.SECRET,
                timestamp=str(int(now)),
                signature=sig,
                body=body,
                now=now,
            )

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(_attempt, range(16)))

        assert all(r is None for r in results)
