"""Tests for the session-pulse survey's Aperture backend proxy (feedback.py).

Covers the dashboard-user guard (app tokens refused), the egress consent gate
(the survey rides ``beacon.telemetry_permitted``), the per-install identity
(``beacon.install_id``), the rating enum allowlist, the bounded request-body
read, and the capped response read with redirects disabled.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers import feedback

_TEST_IDENTITY = "install-abc123"


class _FakeContent:
    """Minimal stand-in for ``aiohttp.StreamReader``."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def read(self, n: int = -1) -> bytes:
        return self._raw

    async def iter_chunked(self, n: int):
        for i in range(0, len(self._raw), n):
            yield self._raw[i : i + n]


class _FakeResp:
    """Stand-in for the aiohttp response, also its own async context manager."""

    def __init__(self, status: int, *, text_body: str = "", json_body: object = None) -> None:
        self.status = status
        if json_body is not None:
            raw = json.dumps(json_body).encode("utf-8")
        elif text_body:
            raw = text_body.encode("utf-8")
        else:
            raw = b""
        self.content = _FakeContent(raw)

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _FakeSession:
    """Stand-in for ``aiohttp.ClientSession``; ``post``/``get`` return a preset
    :class:`_FakeResp` or raise. Ignores the ``json=`` / ``headers=`` /
    ``allow_redirects=`` / ``timeout=`` kwargs the real calls pass."""

    def __init__(self, resp: _FakeResp | None = None, *, raise_on_call: bool = False) -> None:
        self._resp = resp
        self._raise = raise_on_call

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False

    def post(self, *_a: object, **_kw: object) -> _FakeResp:
        if self._raise:
            raise RuntimeError("simulated network failure")
        assert self._resp is not None
        return self._resp

    def get(self, *_a: object, **_kw: object) -> _FakeResp:
        if self._raise:
            raise RuntimeError("simulated network failure")
        assert self._resp is not None
        return self._resp


def _install_fake_session(
    monkeypatch: pytest.MonkeyPatch,
    resp: _FakeResp | None = None,
    *,
    raise_on_call: bool = False,
) -> None:
    monkeypatch.setattr(
        feedback.aiohttp,
        "ClientSession",
        lambda *_a, **_kw: _FakeSession(resp, raise_on_call=raise_on_call),
    )


def _allow(monkeypatch: pytest.MonkeyPatch, identity: str = _TEST_IDENTITY) -> None:
    """Grant egress consent and pin the install identity, so a route test
    exercises the Aperture path rather than the consent gate."""
    monkeypatch.setattr(feedback, "_telemetry_permitted", lambda *a, **k: True)
    monkeypatch.setattr(feedback, "_survey_identity", lambda: identity)
    # Established user: past the new-user session window, so route tests reach
    # the Aperture path rather than being short-circuited by the window gate.
    monkeypatch.setattr(
        feedback, "get_user_session_count", lambda: feedback.NEW_USER_SESSION_THRESHOLD
    )


def _deny_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feedback, "_telemetry_permitted", lambda *a, **k: False)


def _patch_body(
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: dict | None = None,
    err: web.Response | None = None,
) -> None:
    """Patch ``feedback.read_bounded_json`` to return ``(body, err)`` without
    touching a real request stream."""

    async def _fake(
        _request: web.Request, max_bytes: int = 0
    ) -> tuple[dict | None, web.Response | None]:
        return body, err

    monkeypatch.setattr(feedback, "read_bounded_json", _fake)


def _dashboard_req(method: str, path: str) -> web.Request:
    """A mocked request from a real dashboard user (``app == ""``)."""
    req = make_mocked_request(method, path)
    req["app"] = ""
    return req


class TestRequireDashboardUser:
    """The shared guard refuses app tokens and absent-middleware requests."""

    @pytest.mark.asyncio
    async def test_app_token_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _allow(monkeypatch)
        req = make_mocked_request("GET", "/api/feedback/eligible")
        req["app"] = "some-app"  # app token: app claim is the app's own name
        resp = await feedback.api_feedback_eligible(req)
        assert resp.status == 403
        assert json.loads(resp.body) == {"code": "forbidden"}

    @pytest.mark.asyncio
    async def test_absent_app_key_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _allow(monkeypatch)
        req = make_mocked_request("GET", "/api/feedback/eligible")  # no app claim
        resp = await feedback.api_feedback_eligible(req)
        assert resp.status == 403
        assert json.loads(resp.body) == {"code": "forbidden"}

    @pytest.mark.asyncio
    async def test_denial_is_sel_audited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GPT-2: an app-token denial must leave a SEL decision record naming
        # the endpoint, and an audit failure must never break the 403.
        calls: list[dict] = []

        class _Recorder:
            def log_tool_invocation(self, **kwargs: object) -> None:
                calls.append(kwargs)

        monkeypatch.setattr(feedback._sel_mod, "sel", lambda: _Recorder())
        _allow(monkeypatch)
        req = make_mocked_request("GET", "/api/feedback/eligible")
        req["app"] = "some-app"
        resp = await feedback.api_feedback_eligible(req)
        assert resp.status == 403
        assert calls and calls[0]["outcome"] == "denied"
        assert calls[0]["tool_name"] == "feedback_eligible"


class TestIdentityAndConsentHelpers:
    """The two pure-ish helpers delegate to beacon as expected."""

    def test_survey_identity_is_install_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(feedback.beacon, "install_id", lambda: "id-xyz")
        assert feedback._survey_identity() == "id-xyz"

    def test_telemetry_permitted_reflects_beacon_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Cfg:
            class telemetry:
                beacon_enabled = True

            class dashboard:
                privacy_acked = True

        class _Verdict:
            def __init__(self, ok: bool) -> None:
                self.ok = ok

        seen_audit_tools: list[str] = []
        monkeypatch.setattr(feedback.KiroCrewConfig, "load", staticmethod(lambda: _Cfg()))

        def _tp(*, enabled: bool, acked: bool, audit_tool: str = "") -> "_Verdict":
            seen_audit_tools.append(audit_tool)
            return _Verdict(enabled and acked)

        monkeypatch.setattr(feedback.beacon, "telemetry_permitted", _tp)
        assert feedback._telemetry_permitted() is True

        _Cfg.telemetry.beacon_enabled = False
        assert feedback._telemetry_permitted() is False
        # GPT-2: a caller-supplied audit tool is forwarded so the verdict is
        # recorded in the SEL trail.
        assert feedback._telemetry_permitted("feedback_submit") is False
        assert seen_audit_tools[-1] == "feedback_submit"


class TestConsentGate:
    """When telemetry is not permitted, no endpoint reaches Aperture."""

    @pytest.mark.asyncio
    async def test_eligible_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _deny_telemetry(monkeypatch)
        # No session installed: if the handler tried to egress it would raise.
        resp = await feedback.api_feedback_eligible(_dashboard_req("GET", "/api/feedback/eligible"))
        assert resp.status == 200
        assert json.loads(resp.body) == {"eligible": False}

    @pytest.mark.asyncio
    async def test_submit_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _deny_telemetry(monkeypatch)
        resp = await feedback.api_feedback_submit(_dashboard_req("POST", "/api/feedback/submit"))
        assert resp.status == 403
        assert json.loads(resp.body) == {"code": "telemetry_disabled"}


class TestNewUserSessionWindow:
    """The survey stays hidden until the install has >= NEW_USER_SESSION_THRESHOLD
    genuine user sessions, and that gate short-circuits before Aperture."""

    def _req(self) -> web.Request:
        return _dashboard_req("GET", "/api/feedback/eligible")

    @pytest.mark.asyncio
    async def test_under_threshold_not_eligible_even_if_aperture_would_say_yes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow(monkeypatch)
        # One short of the window.
        monkeypatch.setattr(
            feedback, "get_user_session_count", lambda: feedback.NEW_USER_SESSION_THRESHOLD - 1
        )
        # Aperture WOULD return eligible; if the gate is working we never reach it.
        _install_fake_session(monkeypatch, _FakeResp(200, json_body={"prompt": "x"}))
        resp = await feedback.api_feedback_eligible(self._req())
        assert resp.status == 200
        assert json.loads(resp.body) == {"eligible": False}

    @pytest.mark.asyncio
    async def test_at_threshold_reaches_aperture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _allow(monkeypatch)
        monkeypatch.setattr(
            feedback, "get_user_session_count", lambda: feedback.NEW_USER_SESSION_THRESHOLD
        )
        _install_fake_session(monkeypatch, _FakeResp(200, json_body={"prompt": "x"}))
        resp = await feedback.api_feedback_eligible(self._req())
        assert resp.status == 200
        assert json.loads(resp.body) == {"eligible": True}

    @pytest.mark.asyncio
    async def test_past_window_but_server_dedup_not_due_stays_not_eligible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The session+cooldown combo at the server layer: the new-user window is
        # cleared (count == threshold), but Aperture's own per-user dedup says
        # "not due yet" (null body) -- the server-side analog of the 30-day
        # cooldown. Clearing the session window must NOT override it; both gates
        # are independent, so the survey stays not-eligible. (The browser's
        # 30-day localStorage cooldown is a separate client gate, proven in
        # SessionPulseSurveyCard.test.tsx; the backend never sees it.)
        _allow(monkeypatch)  # session count == NEW_USER_SESSION_THRESHOLD
        _install_fake_session(monkeypatch, _FakeResp(200, json_body=None))
        resp = await feedback.api_feedback_eligible(self._req())
        assert resp.status == 200
        assert json.loads(resp.body) == {"eligible": False}


class TestCustomerResponses:
    """Unit coverage for the pure ``_customer_responses`` builder."""

    def test_missing_rating_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            feedback._customer_responses({})

    def test_blank_rating_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            feedback._customer_responses({"rating": "   "})

    def test_rating_not_in_enum_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            feedback._customer_responses({"rating": "AKIAIOSFODNN7EXAMPLE http://x/?d="})

    def test_every_wire_value_is_accepted(self) -> None:
        for value in ("Very Poor", "Poor", "Fair", "Good", "Excellent"):
            out = feedback._customer_responses({"rating": value})
            assert out[0]["response"]["responseValue"] == value

    def test_rating_only_produces_single_radio_response(self) -> None:
        out = feedback._customer_responses({"rating": "Good"})
        assert out == [
            {
                "question": feedback._RATING_QUESTION,
                "pii": False,
                "response": {"responseType": "radio", "responseValue": "Good"},
            }
        ]

    def test_rating_is_stripped(self) -> None:
        out = feedback._customer_responses({"rating": "  Excellent  "})
        assert out[0]["response"]["responseValue"] == "Excellent"

    def test_feedback_appended_as_textarea_non_pii(self) -> None:
        out = feedback._customer_responses({"rating": "Fair", "feedback": "it was ok"})
        assert len(out) == 2
        fb = out[1]
        assert fb["question"] == feedback._FEEDBACK_QUESTION
        assert fb["pii"] is False
        assert fb["response"] == {"responseType": "textArea", "responseValue": "it was ok"}

    def test_email_appended_as_text_pii(self) -> None:
        out = feedback._customer_responses({"rating": "Good", "email": "a@b.com"})
        assert len(out) == 2
        em = out[1]
        assert em["question"] == feedback._EMAIL_QUESTION
        assert em["pii"] is True
        assert em["response"] == {"responseType": "text", "responseValue": "a@b.com"}

    def test_all_three_answers_in_order(self) -> None:
        out = feedback._customer_responses(
            {"rating": "Excellent", "feedback": "great", "email": "a@b.com"}
        )
        assert [r["response"]["responseType"] for r in out] == ["radio", "textArea", "text"]

    def test_blank_feedback_and_email_are_dropped(self) -> None:
        out = feedback._customer_responses({"rating": "Poor", "feedback": "   ", "email": "  "})
        assert len(out) == 1

    def test_feedback_and_email_are_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def _spy(text: str) -> tuple[str, int]:
            calls.append(text)
            return (f"<redacted:{text}>", 1)

        monkeypatch.setattr(feedback, "redact_exfiltration_urls", _spy)
        monkeypatch.setattr(feedback, "redact_credentials", _spy)

        out = feedback._customer_responses(
            {"rating": "Good", "feedback": "secret", "email": "e@x.com"}
        )
        assert "secret" in calls
        assert "e@x.com" in calls
        assert out[1]["response"]["responseValue"].startswith("<redacted:")
        assert out[2]["response"]["responseValue"].startswith("<redacted:")


class TestApiFeedbackSubmit:
    """Route-level coverage for ``api_feedback_submit`` and its failure modes."""

    @pytest.mark.asyncio
    async def test_app_token_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _allow(monkeypatch)
        req = make_mocked_request("POST", "/api/feedback/submit")
        req["app"] = "some-app"
        resp = await feedback.api_feedback_submit(req)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_bounded_read_error_is_passed_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow(monkeypatch)
        too_large = web.json_response(
            {"error": "payload too large", "code": "payload_too_large"}, status=413
        )
        _patch_body(monkeypatch, err=too_large)
        resp = await feedback.api_feedback_submit(_dashboard_req("POST", "/api/feedback/submit"))
        assert resp.status == 413
        assert json.loads(resp.body) == {"error": "payload too large", "code": "payload_too_large"}

    @pytest.mark.asyncio
    async def test_missing_rating_returns_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _allow(monkeypatch)
        _patch_body(monkeypatch, body={"feedback": "hi"})
        resp = await feedback.api_feedback_submit(_dashboard_req("POST", "/api/feedback/submit"))
        assert resp.status == 400
        assert json.loads(resp.body) == {"code": "missing_rating"}

    @pytest.mark.asyncio
    async def test_off_enum_rating_returns_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _allow(monkeypatch)
        _patch_body(monkeypatch, body={"rating": "not-a-real-option"})
        resp = await feedback.api_feedback_submit(_dashboard_req("POST", "/api/feedback/submit"))
        assert resp.status == 400
        assert json.loads(resp.body) == {"code": "missing_rating"}

    @pytest.mark.asyncio
    async def test_success_returns_ok_and_sends_install_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}

        class _CapResp:
            status = 200
            content = _FakeContent(b"{}")

            async def __aenter__(self) -> "_CapResp":
                return self

            async def __aexit__(self, *_a: object) -> bool:
                return False

        class _CapSession:
            async def __aenter__(self) -> "_CapSession":
                return self

            async def __aexit__(self, *_a: object) -> bool:
                return False

            def post(self, _url: str, *, json: object = None, **_kw: object) -> _CapResp:
                captured["json"] = json
                return _CapResp()

        _allow(monkeypatch, identity="install-999")
        monkeypatch.setattr(feedback.aiohttp, "ClientSession", lambda *_a, **_kw: _CapSession())
        _patch_body(monkeypatch, body={"rating": "Good", "sessionId": "chat-1-2"})
        resp = await feedback.api_feedback_submit(_dashboard_req("POST", "/api/feedback/submit"))
        assert resp.status == 200
        assert json.loads(resp.body) == {"ok": True}
        meta = {m["key"]: m["value"] for m in captured["json"]["metadataList"]}
        assert meta["userId"] == "install-999"

    @pytest.mark.asyncio
    async def test_aperture_non_2xx_returns_502_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow(monkeypatch)
        _install_fake_session(monkeypatch, _FakeResp(400, text_body="bad form"))
        _patch_body(monkeypatch, body={"rating": "Good"})
        resp = await feedback.api_feedback_submit(_dashboard_req("POST", "/api/feedback/submit"))
        assert resp.status == 502
        assert json.loads(resp.body) == {"code": "aperture_rejected"}

    @pytest.mark.asyncio
    async def test_aperture_unreachable_returns_502_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow(monkeypatch)
        _install_fake_session(monkeypatch, raise_on_call=True)
        _patch_body(monkeypatch, body={"rating": "Good"})
        resp = await feedback.api_feedback_submit(_dashboard_req("POST", "/api/feedback/submit"))
        assert resp.status == 502
        assert json.loads(resp.body) == {"code": "aperture_unreachable"}


class TestApiFeedbackEligible:
    """Route-level coverage for ``api_feedback_eligible`` \u2014 fails CLOSED."""

    @pytest.mark.asyncio
    async def test_app_token_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _allow(monkeypatch)
        req = make_mocked_request("GET", "/api/feedback/eligible")
        req["app"] = "some-app"
        resp = await feedback.api_feedback_eligible(req)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_empty_identity_returns_not_eligible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow(monkeypatch, identity="")
        resp = await feedback.api_feedback_eligible(self._req())
        assert resp.status == 200
        assert json.loads(resp.body) == {"eligible": False}

    def _req(self) -> web.Request:
        return _dashboard_req("GET", "/api/feedback/eligible")

    @pytest.mark.asyncio
    async def test_eligible_when_aperture_returns_non_null_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow(monkeypatch)
        _install_fake_session(monkeypatch, _FakeResp(200, json_body={"prompt": "x"}))
        resp = await feedback.api_feedback_eligible(self._req())
        assert resp.status == 200
        assert json.loads(resp.body) == {"eligible": True}

    @pytest.mark.asyncio
    async def test_not_eligible_when_aperture_returns_null_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow(monkeypatch)
        _install_fake_session(monkeypatch, _FakeResp(200, json_body=None))
        resp = await feedback.api_feedback_eligible(self._req())
        assert resp.status == 200
        assert json.loads(resp.body) == {"eligible": False}

    @pytest.mark.asyncio
    async def test_not_eligible_on_non_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _allow(monkeypatch)
        _install_fake_session(monkeypatch, _FakeResp(503))
        resp = await feedback.api_feedback_eligible(self._req())
        assert resp.status == 200
        assert json.loads(resp.body) == {"eligible": False}

    @pytest.mark.asyncio
    async def test_not_eligible_when_request_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _allow(monkeypatch)
        _install_fake_session(monkeypatch, raise_on_call=True)
        resp = await feedback.api_feedback_eligible(self._req())
        assert resp.status == 200
        assert json.loads(resp.body) == {"eligible": False}

    @pytest.mark.asyncio
    async def test_eligible_sends_install_id_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        class _CapResp:
            status = 200
            content = _FakeContent(b'{"prompt": "x"}')

            async def __aenter__(self) -> "_CapResp":
                return self

            async def __aexit__(self, *_a: object) -> bool:
                return False

        class _CapSession:
            async def __aenter__(self) -> "_CapSession":
                return self

            async def __aexit__(self, *_a: object) -> bool:
                return False

            def get(self, _url: str, *, headers: object = None, **_kw: object) -> _CapResp:
                captured["headers"] = headers
                return _CapResp()

        _allow(monkeypatch, identity="install-555")
        monkeypatch.setattr(feedback.aiohttp, "ClientSession", lambda *_a, **_kw: _CapSession())
        resp = await feedback.api_feedback_eligible(self._req())
        assert resp.status == 200
        assert captured["headers"]["userid"] == "install-555"


class TestSetupFeedbackRoutes:
    """The route registration helper wires all three endpoints."""

    def test_registers_all_routes(self) -> None:
        app = web.Application()
        feedback.setup_feedback_routes(app)
        registered = {(route.method, route.resource.canonical) for route in app.router.routes()}
        assert ("POST", "/api/feedback/submit") in registered
        assert ("GET", "/api/feedback/eligible") in registered
        # The per-install identity endpoint was removed: identity is
        # server-authoritative (install_id), never fetched by the client.
        assert ("GET", "/api/feedback/identity") not in registered


class TestSubmitMetadataRedaction:
    """GPT bf59b21c #1: client-supplied ``sessionId`` / ``kiroCrewVersion`` land
    in Aperture's ``metadataList`` and must be redacted before egress, the same
    way ``feedback`` / ``email`` already are -- a credential-bearing custom slot
    key must not leave the host verbatim. ``userId`` is the server-derived
    install id and is intentionally NOT redacted."""

    def _req(self) -> web.Request:
        return _dashboard_req("POST", "/api/feedback/submit")

    def _capture(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        """Install a ClientSession whose POST records the outgoing ``json=``
        payload and returns a 2xx, so the built metadataList is inspectable."""
        captured: dict = {}

        class _CapResp:
            status = 200
            content = _FakeContent(b"")

            async def __aenter__(self) -> "_CapResp":
                return self

            async def __aexit__(self, *_a: object) -> bool:
                return False

        class _CapSession:
            async def __aenter__(self) -> "_CapSession":
                return self

            async def __aexit__(self, *_a: object) -> bool:
                return False

            def post(self, _url: str, *, json: object = None, **_kw: object) -> _CapResp:
                captured["payload"] = json
                return _CapResp()

        monkeypatch.setattr(feedback.aiohttp, "ClientSession", lambda *_a, **_kw: _CapSession())
        return captured

    @staticmethod
    def _meta(payload: object, key: str) -> str:
        entries = {m["key"]: m["value"] for m in payload["metadataList"]}  # type: ignore[index]
        return entries[key]

    @pytest.mark.asyncio
    async def test_session_id_and_version_pass_through_both_redactors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Spy on the two redactors so the assertion is independent of which
        # patterns they happen to match: prove the handler routes BOTH
        # sessionId and kiroCrewVersion through exfiltration-URL redaction and
        # then credential redaction (same order as feedback/email), and that
        # the redacted output is what lands in metadataList. No feedback/email
        # in the body, so the only inputs these spies see are the two metadata
        # fields under test.
        seen_exfil: list[str] = []
        seen_cred: list[str] = []

        def _spy_exfil(s: str) -> tuple[str, int]:
            seen_exfil.append(s)
            return s.replace("URLBIT", "[url]"), 1

        def _spy_cred(s: str) -> tuple[str, int]:
            seen_cred.append(s)
            return s.replace("CREDBIT", "[cred]"), 1

        monkeypatch.setattr(feedback, "redact_exfiltration_urls", _spy_exfil)
        monkeypatch.setattr(feedback, "redact_credentials", _spy_cred)
        _allow(monkeypatch)
        captured = self._capture(monkeypatch)
        _patch_body(
            monkeypatch,
            body={
                "rating": "Good",
                "sessionId": "chat-URLBIT-CREDBIT",
                "kiroCrewVersion": "URLBIT-CREDBIT",
            },
        )

        resp = await feedback.api_feedback_submit(self._req())
        assert resp.status == 200

        # Both fields were run through exfiltration redaction first...
        assert "chat-URLBIT-CREDBIT" in seen_exfil
        assert "URLBIT-CREDBIT" in seen_exfil
        # ...then credential redaction saw the exfil-redacted form.
        assert "chat-[url]-CREDBIT" in seen_cred
        assert "[url]-CREDBIT" in seen_cred
        # ...and the fully redacted value is what egresses.
        assert self._meta(captured["payload"], "sessionId") == "chat-[url]-[cred]"
        assert self._meta(captured["payload"], "kiro_crew_version") == "[url]-[cred]"

    @pytest.mark.asyncio
    async def test_clean_metadata_passes_through_and_user_id_is_server_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Redaction must not corrupt an ordinary slot key / version, and the
        # server-derived install id is forwarded unmodified as userId.
        _allow(monkeypatch, identity="install-xyz")
        captured = self._capture(monkeypatch)
        _patch_body(
            monkeypatch,
            body={
                "rating": "Good",
                "sessionId": "chat-7-1786950000",
                "kiroCrewVersion": "1.2.3",
            },
        )

        resp = await feedback.api_feedback_submit(self._req())
        assert resp.status == 200
        assert self._meta(captured["payload"], "sessionId") == "chat-7-1786950000"
        assert self._meta(captured["payload"], "kiro_crew_version") == "1.2.3"
        assert self._meta(captured["payload"], "userId") == "install-xyz"
