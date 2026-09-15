"""Tests for the trusted mediated-request dispatcher.

The load-bearing property under test: the secret's PLAINTEXT is used only to
build the outbound request and never appears in the tool result, an error, a
log record, or the SEL audit. A canary value is stored and asserted absent from
every observable surface.
"""

from __future__ import annotations

import json
import socket

import pytest
import requests

from kiro_crew.secrets import SecretVault
from kiro_crew.secrets_mediation import dispatch
from kiro_crew.secrets_mediation.dispatch import (
    MediatedRequest,
    MediationError,
    perform_mediated_request,
)
from kiro_crew.secrets_mediation.policy import POLICY_FILENAME, PolicyError
from kiro_crew.secrets_mediation.ssrf import SsrfError

CANARY = "sk-canary-3f9a2b7c1d8e-DO-NOT-LEAK"


def _seed_vault(config_dir, name=..., value=CANARY):
    SecretVault(config_dir).set_sync("WEATHER_API_KEY" if name is ... else name, value)


def _write_policy(config_dir, authorizations):
    from kiro_crew.secrets_mediation.provenance import SIGNATURE_FIELD, sign_authorizations

    sig = sign_authorizations(authorizations, config_dir)
    (config_dir / POLICY_FILENAME).write_text(
        json.dumps({"version": 1, "authorizations": authorizations, SIGNATURE_FIELD: sig}),
        encoding="utf-8",
    )


def _authorize(config_dir, origin="https://api.weather.example", placement=None):
    _write_policy(
        config_dir,
        {"WEATHER_API_KEY": {"origin": origin, "placement": placement or {"type": "bearer"}}},
    )


class _FakeResponse:
    def __init__(self, status=200, headers=None, body=b"{}", ctype="application/json"):
        self.status_code = status
        self.headers = {"content-type": ctype, **(headers or {})}
        self._body = body
        self.encoding = "utf-8"

    def iter_content(self, chunk_size=65536):
        yield self._body

    @property
    def content(self):
        return self._content if hasattr(self, "_content") else self._body

    def close(self):
        pass


def _stub_network(monkeypatch, capture, *, response=None, redirect_to=None):
    """Stub the whole Session.request path and record what headers were sent."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda h, p, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))],
    )

    import requests

    def _fake_request(
        self, *, method, url, params, headers, json, timeout, allow_redirects, stream
    ):
        capture.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "json": json,
                "timeout": timeout,
            }
        )
        if redirect_to and len(capture) == 1:
            return _FakeResponse(status=302, headers={"location": redirect_to})
        return response or _FakeResponse()

    monkeypatch.setattr(requests.Session, "request", _fake_request)
    # Mounting the pinned adapter touches urllib3 internals we don't exercise here.
    monkeypatch.setattr(requests.Session, "mount", lambda self, *a, **k: None)


def test_bearer_secret_injected_but_absent_from_result(tmp_path, monkeypatch):
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    cap = []
    _stub_network(monkeypatch, cap, response=_FakeResponse(body=b'{"ok":true}'))

    resp = perform_mediated_request(
        MediatedRequest(
            secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/v1/now"
        ),
        tmp_path,
    )
    # The secret WAS injected into the outbound Authorization header...
    assert cap[0]["headers"]["Authorization"] == f"Bearer {CANARY}"
    # ...and is ABSENT from everything the caller/model can observe.
    blob = json.dumps({"status": resp.status, "headers": resp.headers, "body": resp.body})
    assert CANARY not in blob
    assert "Authorization" not in resp.headers  # request auth never echoed back


def test_header_placement_injects_named_header(tmp_path, monkeypatch):
    _seed_vault(tmp_path)
    _authorize(tmp_path, placement={"type": "header", "header": "X-Api-Key"})
    cap = []
    _stub_network(monkeypatch, cap)
    perform_mediated_request(
        MediatedRequest(
            secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/x"
        ),
        tmp_path,
    )
    assert cap[0]["headers"]["X-Api-Key"] == CANARY
    assert "Authorization" not in cap[0]["headers"]


def test_unauthorized_origin_fails_before_secret_read(tmp_path, monkeypatch):
    _seed_vault(tmp_path)
    _authorize(tmp_path, origin="https://api.weather.example")
    # Prove the vault is never read on the refusal path: make reveal() explode.
    monkeypatch.setattr(SecretVault, "get", lambda self, name: pytest.fail("secret was read"))
    cap = []
    _stub_network(monkeypatch, cap)
    with pytest.raises(PolicyError):
        perform_mediated_request(
            MediatedRequest(
                secret_name="WEATHER_API_KEY", method="GET", url="https://evil.example.com/steal"
            ),
            tmp_path,
        )
    assert cap == []  # no network either


def test_userinfo_url_refused_before_secret_read(tmp_path, monkeypatch):
    """A ``userinfo@host`` authority (e.g. https://authorized@evil/) is a classic
    origin-confusion vector — refused before any vault read or network."""
    from kiro_crew.secrets_mediation.dispatch import MediationError

    _seed_vault(tmp_path)
    _authorize(tmp_path, origin="https://api.weather.example")
    monkeypatch.setattr(SecretVault, "get", lambda self, name: pytest.fail("secret was read"))
    cap = []
    _stub_network(monkeypatch, cap)
    with pytest.raises(MediationError):
        perform_mediated_request(
            MediatedRequest(
                secret_name="WEATHER_API_KEY",
                method="GET",
                url="https://api.weather.example@evil.example.com/steal",
            ),
            tmp_path,
        )
    assert cap == []


def test_backslash_url_refused_before_secret_read(tmp_path, monkeypatch):
    """A backslash can move the host boundary in some HTTP stacks — refused."""
    from kiro_crew.secrets_mediation.dispatch import MediationError

    _seed_vault(tmp_path)
    _authorize(tmp_path, origin="https://api.weather.example")
    monkeypatch.setattr(SecretVault, "get", lambda self, name: pytest.fail("secret was read"))
    cap = []
    _stub_network(monkeypatch, cap)
    with pytest.raises(MediationError):
        perform_mediated_request(
            MediatedRequest(
                secret_name="WEATHER_API_KEY",
                method="GET",
                url="https://api.weather.example\\@evil.example.com/",
            ),
            tmp_path,
        )
    assert cap == []


def test_redirect_hops_share_one_end_to_end_deadline(tmp_path, monkeypatch):
    """The per-hop request timeout must be the REMAINING end-to-end budget, not
    the full timeout re-applied on every hop — otherwise a redirect chain could
    run up to timeout × (max_redirects+1) and blow past the caller's loopback
    timeout. Each hop's timeout must be <= the original and not increase."""
    _seed_vault(tmp_path)
    # Same-origin redirect so the chain proceeds through multiple hops.
    _authorize(tmp_path, origin="https://api.weather.example")
    cap = []
    _stub_network(
        monkeypatch,
        cap,
        redirect_to="https://api.weather.example/next",
    )
    perform_mediated_request(
        MediatedRequest(
            secret_name="WEATHER_API_KEY",
            method="GET",
            url="https://api.weather.example/start",
            timeout_s=30.0,
        ),
        tmp_path,
    )
    timeouts = [c["timeout"] for c in cap]
    assert len(timeouts) >= 2, "expected at least one redirect hop"
    # Every hop is bounded by the original budget and never grows hop-to-hop.
    assert all(0 < t <= 30.0 for t in timeouts)
    assert timeouts == sorted(timeouts, reverse=True) or len(set(timeouts)) == 1


def test_non_finite_timeout_refused_before_secret_read(tmp_path, monkeypatch):
    """A NaN/Infinity timeout must be refused cleanly — otherwise it propagates
    into the end-to-end deadline math and breaks the timeout. Fails before the
    vault is read."""
    from kiro_crew.secrets_mediation.dispatch import MediationError

    _seed_vault(tmp_path)
    _authorize(tmp_path, origin="https://api.weather.example")
    monkeypatch.setattr(SecretVault, "get", lambda self, name: pytest.fail("secret was read"))
    cap = []
    _stub_network(monkeypatch, cap)
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(MediationError):
            perform_mediated_request(
                MediatedRequest(
                    secret_name="WEATHER_API_KEY",
                    method="GET",
                    url="https://api.weather.example/x",
                    timeout_s=bad,
                ),
                tmp_path,
            )
    assert cap == []


def test_unicode_transport_error_becomes_a_secret_free_mediation_error(tmp_path, monkeypatch):
    """A non-latin-1 injected credential makes the transport raise UnicodeError
    (a ValueError, not a RequestException). It must become a secret-free
    MediationError, never escape as a raw crash that could carry the value."""
    import requests

    from kiro_crew.secrets_mediation.dispatch import MediationError

    _seed_vault(tmp_path)
    _authorize(tmp_path, origin="https://api.weather.example")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda h, p, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))],
    )
    monkeypatch.setattr(requests.Session, "mount", lambda self, *a, **k: None)

    def _raise_unicode(self, **kwargs):
        raise UnicodeEncodeError("latin-1", "\u2603", 0, 1, "not latin-1")

    monkeypatch.setattr(requests.Session, "request", _raise_unicode)
    with pytest.raises(MediationError) as caught:
        perform_mediated_request(
            MediatedRequest(
                secret_name="WEATHER_API_KEY",
                method="GET",
                url="https://api.weather.example/x",
            ),
            tmp_path,
        )
    # Secret-free, and the offending-bytes cause is not chained.
    assert "could not be encoded" in str(caught.value)
    assert caught.value.__cause__ is None


def test_no_authorization_fails_closed(tmp_path, monkeypatch):
    _seed_vault(tmp_path)  # secret exists but no policy authorizes it
    monkeypatch.setattr(SecretVault, "get", lambda self, name: pytest.fail("secret was read"))
    with pytest.raises(PolicyError):
        perform_mediated_request(
            MediatedRequest(
                secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/x"
            ),
            tmp_path,
        )


def test_redirect_to_different_origin_refused(tmp_path, monkeypatch):
    _seed_vault(tmp_path)
    _authorize(tmp_path, origin="https://api.weather.example")
    cap = []
    _stub_network(monkeypatch, cap, redirect_to="https://evil.example.com/creds")
    with pytest.raises(PolicyError):
        perform_mediated_request(
            MediatedRequest(
                secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/redir"
            ),
            tmp_path,
        )
    # Exactly one request was made; the credential never followed the redirect.
    assert len(cap) == 1


def test_disallowed_method_fails_closed(tmp_path, monkeypatch):
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    monkeypatch.setattr(SecretVault, "get", lambda self, name: pytest.fail("secret was read"))
    with pytest.raises(MediationError):
        perform_mediated_request(
            MediatedRequest(
                secret_name="WEATHER_API_KEY", method="TRACE", url="https://api.weather.example/x"
            ),
            tmp_path,
        )


def test_ssrf_blocks_before_secret_read(tmp_path, monkeypatch):
    _seed_vault(tmp_path)
    _authorize(tmp_path, origin="https://api.weather.example")
    # Authorized origin, but it resolves to a private address -> SSRF refuse,
    # and the secret must not be read.
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda h, p, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", p))],
    )
    monkeypatch.setattr(SecretVault, "get", lambda self, name: pytest.fail("secret was read"))
    with pytest.raises(SsrfError):
        perform_mediated_request(
            MediatedRequest(
                secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/x"
            ),
            tmp_path,
        )


def test_secret_absent_from_error_on_transport_failure(tmp_path, monkeypatch):
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda h, p, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))],
    )
    import requests

    def _boom(self, **kwargs):
        raise requests.ConnectionError("connection reset")

    monkeypatch.setattr(requests.Session, "request", _boom)
    monkeypatch.setattr(requests.Session, "mount", lambda self, *a, **k: None)
    with pytest.raises(MediationError) as ei:
        perform_mediated_request(
            MediatedRequest(
                secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/x"
            ),
            tmp_path,
        )
    assert CANARY not in str(ei.value)


def test_response_body_size_capped(tmp_path, monkeypatch):
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    cap = []
    big = b"x" * (dispatch._MAX_RESPONSE_BYTES + 5000)
    _stub_network(monkeypatch, cap, response=_FakeResponse(body=big, ctype="text/plain"))
    resp = perform_mediated_request(
        MediatedRequest(
            secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/big"
        ),
        tmp_path,
    )
    assert resp.truncated is True
    assert len(resp.body) <= dispatch._MAX_RESPONSE_BYTES


def test_reflected_secret_is_scrubbed_from_body_and_headers(tmp_path, monkeypatch):
    """An upstream that ECHOES the credential must not leak it back to the agent.

    Some endpoints reflect the Authorization header in their body or in a
    response header (debug/echo routes, some error payloads). The sanitized
    result the agent receives must never carry the plaintext, so the exact secret
    value is scrubbed from both body and returned header values. Fails without the
    scrub in ``_sanitize_response``.
    """
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    cap = []
    reflected_body = ('{"you_sent":"Bearer ' + CANARY + '"}').encode("utf-8")
    # ``etag`` is on the safe-header allowlist, so a reflected value there would
    # otherwise survive; prove it is scrubbed too.
    resp_stub = _FakeResponse(body=reflected_body, headers={"etag": CANARY})
    _stub_network(monkeypatch, cap, response=resp_stub)
    resp = perform_mediated_request(
        MediatedRequest(
            secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/echo"
        ),
        tmp_path,
    )
    assert CANARY not in resp.body
    assert all(CANARY not in v for v in resp.headers.values())
    assert "[redacted-secret]" in resp.body


def test_encoded_reflected_secret_is_scrubbed(tmp_path, monkeypatch):
    """A reflected credential in a common reversible encoding is also scrubbed.

    An echo endpoint that base64- or hex-encodes what it received would slip past
    an exact-plaintext scrub. The scrub covers the frequent encodings, so a
    decodable copy does not reach the agent. (Not a completeness claim — an
    arbitrary transform cannot be enumerated; the body is also content-type and
    size bounded.)
    """
    import base64

    _seed_vault(tmp_path)
    _authorize(tmp_path)
    cap = []
    b64 = base64.b64encode(CANARY.encode()).decode()
    hexed = CANARY.encode().hex()
    reflected = ('{"b64":"' + b64 + '","hex":"' + hexed + '"}').encode("utf-8")
    _stub_network(monkeypatch, cap, response=_FakeResponse(body=reflected))
    resp = perform_mediated_request(
        MediatedRequest(
            secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/echo"
        ),
        tmp_path,
    )
    assert b64 not in resp.body
    assert hexed not in resp.body


def test_short_reflected_secret_is_still_scrubbed(tmp_path, monkeypatch):
    """A secret shorter than the derived-encoding length floor must still be
    scrubbed when reflected verbatim — the floor applies only to derived
    encodings, never to the raw value."""
    _seed_vault(tmp_path, value="pw12")  # 4 chars, below the 8-char derived floor
    _authorize(tmp_path)
    cap = []
    _stub_network(monkeypatch, cap, response=_FakeResponse(body=b'{"echo":"you sent pw12 as key"}'))
    resp = perform_mediated_request(
        MediatedRequest(
            secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/echo"
        ),
        tmp_path,
    )
    assert "pw12" not in resp.body
    assert "[redacted-secret]" in resp.body


def test_json_escaped_reflected_secret_is_scrubbed(tmp_path, monkeypatch):
    """A secret with `"`/`\\` reflected inside a JSON string is JSON-escaped by
    the origin; the scrub must catch the escaped form too."""
    _seed_vault(tmp_path, value='ab"c\\d')
    _authorize(tmp_path)
    cap = []
    import json as _j

    # What an echo endpoint returns: the secret inside a JSON string value.
    reflected = _j.dumps({"echo": 'ab"c\\d'}).encode("utf-8")
    _stub_network(monkeypatch, cap, response=_FakeResponse(body=reflected))
    resp = perform_mediated_request(
        MediatedRequest(
            secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/echo"
        ),
        tmp_path,
    )
    assert 'ab"c\\d' not in resp.body
    assert 'ab\\"c\\\\d' not in resp.body  # the JSON-escaped form is gone too


def test_redirect_does_not_replay_a_post_mutation(tmp_path, monkeypatch):
    """A 301/302/303 on ANY unsafe method (POST/PUT/PATCH/DELETE) must degrade to
    GET with no body, so an external mutation is never executed twice by the
    redirect follow. _stub_network's redirect uses 302."""
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        _seed_vault(tmp_path)
        _authorize(tmp_path)
        cap = []
        _stub_network(monkeypatch, cap, redirect_to="https://api.weather.example/done")
        perform_mediated_request(
            MediatedRequest(
                secret_name="WEATHER_API_KEY",
                method=verb,
                url="https://api.weather.example/submit",
                json_body={"x": 1},
            ),
            tmp_path,
        )
        assert len(cap) == 2, f"{verb}: expected one redirect hop"
        first, second = cap
        assert first["method"] == verb and first["json"] == {"x": 1}
        # The replayed hop must NOT repeat the mutating method or body.
        assert second["method"] == "GET", f"{verb}: redirect must degrade to GET"
        assert second["json"] is None, f"{verb}: redirect must drop the body"


def test_pinned_adapter_does_not_mutate_urllib3_globals(monkeypatch):
    """The DNS pin must be per-connection, never a process-global resolver swap.

    The host runs ``perform_mediated_request`` via ``asyncio.to_thread``, so two
    mediated requests can dispatch concurrently. A global
    ``urllib3.util.connection.create_connection`` swap would let one send's
    teardown restore/replace another's resolver, reopening the DNS-rebinding
    window with the credential in flight. Building and mounting the adapter must
    leave the global untouched. Fails if the adapter reverts to the global swap.
    """
    from urllib3.util import connection as urllib3_connection

    before = urllib3_connection.create_connection
    adapter = dispatch._PinnedHTTPSAdapter("93.184.216.34", max_retries=0)
    session = requests.Session()
    session.mount("https://", adapter)
    try:
        assert urllib3_connection.create_connection is before, (
            "the pinned adapter mutated the urllib3 module-global resolver; "
            "concurrent mediated requests would race on it"
        )
        # The pin lives on the adapter's own PoolManager, per-instance.
        pinned_pool_cls = adapter.poolmanager.pool_classes_by_scheme["https"]
        assert pinned_pool_cls is not None
        assert pinned_pool_cls.__name__ == "_PinnedHTTPSConnectionPool"
    finally:
        session.close()
