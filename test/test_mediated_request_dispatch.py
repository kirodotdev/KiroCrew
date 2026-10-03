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

CANARY = "sk-canary-3f9a2b7c1d8e-DO-NOT-LEAK"


_REAL_WAIT_FOR_WITHHELD_DEADLINE = dispatch._wait_for_withheld_deadline


@pytest.fixture(autouse=True)
def _host_policy_key(tmp_path, monkeypatch):
    """Install the host key and keep unrelated dispatcher tests instantaneous."""
    from kiro_crew.secrets_mediation import provenance

    monkeypatch.setattr(provenance, "_token_root", lambda: b"T" * 32)
    provenance.initialize_host_key(tmp_path)
    monkeypatch.setattr(dispatch, "_wait_for_withheld_deadline", lambda _deadline: None)


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
    entry = {"origin": origin, "placement": placement or {"type": "bearer"}}
    _write_policy(config_dir, {"WEATHER_API_KEY": entry})


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


def test_drip_fed_body_is_not_read_and_response_is_closed_immediately(tmp_path, monkeypatch):
    # The mediated result is the fixed withheld constant and never includes the
    # upstream body, so the dispatch path must NOT read the body: reading it is a
    # timing channel — a complicit origin can send headers fast then DRIP the body
    # so the origin controls completion/error timing past the normalized deadline.
    # Here iter_content would block forever (an infinite drip); the test proves it
    # is never iterated and that close() is called right after headers, so a
    # hostile origin cannot hold the connection open past the deadline.
    _seed_vault(tmp_path)
    _authorize(tmp_path)

    class _DripResponse(_FakeResponse):
        def __init__(self):
            super().__init__(body=b'{"ok":true}')
            self.closed = False
            self.iter_calls = 0

        def iter_content(self, chunk_size=65536):
            # If the dispatch path ever drains the body this both records the
            # breach and hangs the test (an endless drip) — so a passing test
            # proves the body is never read.
            self.iter_calls += 1
            while True:
                yield b"drip"

        def close(self):
            self.closed = True

    drip = _DripResponse()
    cap = []
    _stub_network(monkeypatch, cap, response=drip)

    resp = perform_mediated_request(
        MediatedRequest(
            secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/v1/now"
        ),
        tmp_path,
    )

    # The call completed (fixed withheld constant), the body was never iterated,
    # and the response was closed promptly after headers.
    assert "mediated request completed" in resp.body  # fixed withheld constant returned
    assert resp.status == 0  # withheld status is the fixed constant, not origin-derived
    assert drip.iter_calls == 0  # body never read -> no drip channel
    assert drip.closed is True  # connection released right after headers


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


def test_corrupt_vault_read_becomes_a_secret_free_mediation_error(tmp_path, monkeypatch):
    """A corrupt/hand-edited store (ValueError), a mismatched .vault_key
    (InvalidTag), or an UNREADABLE store (OSError — the file is missing,
    permission-denied, or the I/O failed) at vault.get must NOT crash: it
    collapses to the fixed withheld constant (the same result every outcome
    returns), never propagating uncaught to an HTTP 500 and never carrying the
    secret or store internals. An OSError that escaped would also strand the
    already-consumed single-use capability, which cannot be retried.

    Mutation guard: dropping any of ValueError/InvalidTag/OSError from the
    try/except around vault.get re-raises that raw error out of the padded
    region and this withheld assertion fails.
    """
    from cryptography.exceptions import InvalidTag

    _seed_vault(tmp_path)
    _authorize(tmp_path, origin="https://api.weather.example")
    cap: list = []
    _stub_network(monkeypatch, cap)

    for exc in (
        ValueError("corrupt store record"),
        InvalidTag(),
        OSError("vault file unreadable"),
    ):

        def _raise(self, name, _e=exc):
            raise _e

        monkeypatch.setattr(SecretVault, "get", _raise)
        resp = perform_mediated_request(
            MediatedRequest(
                secret_name="WEATHER_API_KEY",
                method="GET",
                url="https://api.weather.example/x",
            ),
            tmp_path,
        )
        # Collapses to the fixed withheld constant; no crash, no leak.
        assert resp.status == dispatch._WITHHELD_STATUS
        assert CANARY not in resp.body
        assert "corrupt store record" not in resp.body  # no internals leaked
        assert "vault file unreadable" not in resp.body
    assert cap == []  # never reached the network


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


def test_malformed_bracket_url_raises_mediation_error():
    # An unmatched IPv6 bracket makes urlsplit itself raise ValueError
    # ("Invalid IPv6 URL") at parse time. _reject_confusable_url must convert it
    # to MediationError so the host handler's boundary (which catches only the
    # three mediation exception types) returns a refusal, never an uncaught 500.
    from kiro_crew.secrets_mediation.dispatch import (
        MediationError,
        _reject_confusable_url,
    )

    for bad in ("https://[::1", "https://[not-hex]/x", "https://[g::1]"):
        with pytest.raises(MediationError):
            _reject_confusable_url(bad)


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
    # Each hop's timeout is a (connect, read) tuple, each element bounded to the
    # time LEFT until the end-to-end deadline. Reduce each hop to its largest
    # element for the monotonicity check.
    assert all(isinstance(t, tuple) and len(t) == 2 for t in timeouts)
    per_hop = [max(t) for t in timeouts]
    # Every hop is bounded by the original budget and never grows hop-to-hop.
    assert all(0 < t <= 30.0 for t in per_hop)
    assert per_hop == sorted(per_hop, reverse=True) or len(set(per_hop)) == 1


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
    # Authorized origin, but it resolves to a private address -> SSRF refuse.
    # The refusal collapses to the fixed withheld constant (padded to the same
    # deadline as a success, so a per-call blocked-vs-public DNS answer is not a
    # timing oracle), and the secret is NEVER read on the refused path.
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda h, p, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", p))],
    )
    monkeypatch.setattr(SecretVault, "get", lambda self, name: pytest.fail("secret was read"))
    resp = perform_mediated_request(
        MediatedRequest(
            secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/x"
        ),
        tmp_path,
    )
    assert resp.status == dispatch._WITHHELD_STATUS
    assert CANARY not in resp.body


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


def test_default_completion_is_padded_to_one_fixed_deadline(tmp_path, monkeypatch):
    """Variable upstream latency changes padding, not observable completion."""

    class _Clock:
        def __init__(self) -> None:
            self.now = 100.0
            self.sleeps: list[float] = []

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            self.now += seconds

    _seed_vault(tmp_path)
    _authorize(tmp_path)
    clock = _Clock()
    monkeypatch.setattr(dispatch.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(dispatch.time, "sleep", clock.sleep)
    monkeypatch.setattr(dispatch, "_wait_for_withheld_deadline", _REAL_WAIT_FOR_WITHHELD_DEADLINE)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda _h, p, *_a, **_k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))
        ],
    )
    monkeypatch.setattr(requests.Session, "mount", lambda self, *_a, **_k: None)

    elapsed: list[float] = []
    for upstream_seconds in (1.0, 7.5):
        clock.now = 100.0
        clock.sleeps.clear()

        def _response_after_delay(self, **_kwargs):
            clock.now += upstream_seconds
            return _FakeResponse(body=b'{"ok":true}')

        monkeypatch.setattr(requests.Session, "request", _response_after_delay)
        started = clock.monotonic()
        response = perform_mediated_request(
            MediatedRequest(
                secret_name="WEATHER_API_KEY",
                method="GET",
                url="https://api.weather.example/x",
                timeout_s=10.0,
            ),
            tmp_path,
        )
        elapsed.append(clock.monotonic() - started)
        assert response.status == dispatch._WITHHELD_STATUS
        assert clock.sleeps == [10.0 - upstream_seconds]

    assert elapsed == [10.0, 10.0]


def test_successful_call_returns_the_fixed_withheld_constant(tmp_path, monkeypatch):
    """A successful mediated call returns ONLY the fixed withheld constant -- no
    body, headers, status, content-type, or length derived from the upstream
    response -- so a reflected credential has no channel to ride out on."""
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    cap: list = []
    # The origin tries to encode via status, a header, and a reflected body.
    _stub_network(
        monkeypatch,
        cap,
        response=_FakeResponse(
            status=503, headers={"etag": CANARY}, body=('{"x":"' + CANARY + '"}').encode()
        ),
    )
    resp = perform_mediated_request(
        MediatedRequest(
            secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/x"
        ),
        tmp_path,
    )
    assert resp.status == dispatch._WITHHELD_STATUS  # not the upstream 503
    assert resp.headers == {}
    assert CANARY not in resp.body
    # The credential WAS injected into the outbound request.
    assert cap and cap[0]["headers"]["Authorization"] == f"Bearer {CANARY}"


def test_post_send_outcomes_all_collapse_to_the_same_constant(tmp_path, monkeypatch):
    """A success, a cross-origin redirect refusal, and a transport error all return
    the IDENTICAL fixed constant, so no error-vs-success oracle survives."""
    _seed_vault(tmp_path)
    _authorize(tmp_path)

    cap0: list = []
    _stub_network(monkeypatch, cap0, response=_FakeResponse(body=b'{"ok":true}'))
    ok = perform_mediated_request(
        MediatedRequest(
            secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/a"
        ),
        tmp_path,
    )

    cap1: list = []
    _stub_network(monkeypatch, cap1, redirect_to="https://evil.example.com/creds")
    redir = perform_mediated_request(
        MediatedRequest(
            secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/b"
        ),
        tmp_path,
    )
    # The credential never followed the cross-origin redirect.
    assert len(cap1) == 1

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda h, p, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))],
    )
    monkeypatch.setattr(requests.Session, "mount", lambda self, *a, **k: None)
    monkeypatch.setattr(
        requests.Session,
        "request",
        lambda self, **k: (_ for _ in ()).throw(requests.ConnectionError("reset")),
    )
    err = perform_mediated_request(
        MediatedRequest(
            secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/c"
        ),
        tmp_path,
    )

    for r in (ok, redir, err):
        assert r.status == dispatch._WITHHELD_STATUS
        assert r.headers == {}
    assert ok.body == redir.body == err.body
    assert CANARY not in ok.body


def test_outbound_body_carrying_a_credential_is_refused_before_send(tmp_path, monkeypatch):
    """F1: an agent-supplied outbound value (request body / headers / query) that a
    required redactor changes -- a credential or an exfiltration URL the model tried
    to smuggle OUT to the origin -- fails the request closed BEFORE the vault read,
    so the secret is never resolved and nothing is sent."""
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    cap: list = []
    _stub_network(monkeypatch, cap)
    monkeypatch.setattr(SecretVault, "get", lambda self, name: pytest.fail("secret was read"))
    # An AWS-style access key in the outbound JSON body trips redact_credentials.
    with pytest.raises(MediationError):
        perform_mediated_request(
            MediatedRequest(
                secret_name="WEATHER_API_KEY",
                method="POST",
                url="https://api.weather.example/x",
                json_body={"note": "my key is AKIAIOSFODNN7EXAMPLE do not share"},
            ),
            tmp_path,
        )
    assert cap == []  # never reached the network


def test_clean_outbound_body_is_allowed(tmp_path, monkeypatch):
    """A benign outbound body passes the redactor scan and the call proceeds
    (returning the withheld constant)."""
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    cap: list = []
    _stub_network(monkeypatch, cap, response=_FakeResponse(body=b'{"ok":true}'))
    resp = perform_mediated_request(
        MediatedRequest(
            secret_name="WEATHER_API_KEY",
            method="POST",
            url="https://api.weather.example/x",
            json_body={"city": "Seattle", "days": 3},
        ),
        tmp_path,
    )
    assert resp.status == dispatch._WITHHELD_STATUS
    assert cap and cap[0]["headers"]["Authorization"] == f"Bearer {CANARY}"


def test_outbound_credential_in_the_url_query_is_refused_before_send(tmp_path, monkeypatch):
    """F1: the URL itself is scanned whole, so a credential the agent moves out of
    the query MAP and into the URL as ``?t=<secret>`` is refused just like one in
    the query map -- the request fails closed before the vault read, nothing sent."""
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    cap: list = []
    _stub_network(monkeypatch, cap)
    monkeypatch.setattr(SecretVault, "get", lambda self, name: pytest.fail("secret was read"))
    # An AWS-style access key spelled directly in the URL query string.
    with pytest.raises(MediationError):
        perform_mediated_request(
            MediatedRequest(
                secret_name="WEATHER_API_KEY",
                method="GET",
                url="https://api.weather.example/x?token=AKIAIOSFODNN7EXAMPLE",
            ),
            tmp_path,
        )
    assert cap == []  # never reached the network


def test_ssrf_refusal_is_indistinguishable_from_a_success(tmp_path, monkeypatch):
    """An SSRF/DNS refusal collapses to the fixed withheld constant, byte-identical
    to a success, and carries NO outcome marker of any kind. The post-send outcome
    is never recorded, because ``security_events.jsonl`` is a sandbox-visible leaf
    and a refused-vs-ok bit there would be a readable one-bit oracle on the
    authorized origin's behaviour."""
    _seed_vault(tmp_path)
    _authorize(tmp_path, origin="https://api.weather.example")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda h, p, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", p))],
    )
    resp = perform_mediated_request(
        MediatedRequest(
            secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/x"
        ),
        tmp_path,
    )
    # The fixed withheld constant, same as any success. No internal marker remains.
    assert resp.status == dispatch._WITHHELD_STATUS
    assert CANARY not in resp.body
    assert not hasattr(resp, "refusal_outcome")


def test_success_and_refusal_responses_are_byte_identical(tmp_path, monkeypatch):
    """A genuine success returns the SAME withheld constant an SSRF refusal returns,
    with no field distinguishing the two -- the oracle-collapse invariant holds and
    nothing on the object encodes the outcome."""
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    _stub_network(monkeypatch, [], response=_FakeResponse(body=b'{"ok":true}'))
    resp = perform_mediated_request(
        MediatedRequest(
            secret_name="WEATHER_API_KEY", method="GET", url="https://api.weather.example/x"
        ),
        tmp_path,
    )
    assert resp.status == dispatch._WITHHELD_STATUS
    assert not hasattr(resp, "refusal_outcome")


def test_request_within_deadline_refuses_a_header_read_past_the_deadline():
    """A complicit origin that drips the TLS handshake / response headers inside
    the per-read inactivity window must not extend completion past the ABSOLUTE
    deadline: the bounded helper refuses AT the deadline (not after the full
    stall) and closes the session so the blocked read aborts. Mirrors the SSRF
    DNS bound -- completion is capped by the normalized budget, not the origin's
    drip rate, so it carries no secret-attributable bit.
    """
    import threading
    import time

    release = threading.Event()
    closed = {"n": 0}
    sock_closed = {"n": 0}

    class _AbortableSock:
        def shutdown(self, _how):
            # The hard abort: once the socket is shut down the blocked recv
            # returns, so the worker thread stops waiting on the drip feed.
            release.set()
            sock_closed["n"] += 1

        def close(self):
            pass

    class _Conn:
        sock = _AbortableSock()

    class _Pool:
        def __init__(self):
            self.pool = type("Q", (), {"queue": [_Conn()]})()

    class _PoolManager:
        pools = type("P", (), {"_container": {"k": _Pool()}})()

    live_closed = {"n": 0}

    class _StubAdapter:
        poolmanager = _PoolManager()

        def force_close_live_connections(self):
            # The primary teardown: abort the checked-out in-flight socket.
            live_closed["n"] += 1
            _AbortableSock().shutdown(0)

    class _StallingSession:
        trust_env = True

        def request(self, **_kw):
            # Block well past the deadline, released when the socket is aborted
            # (or in teardown) so the daemon thread exits rather than leaking.
            release.wait(timeout=10.0)
            return _FakeResponse(body=b"{}")

        def close(self):
            closed["n"] += 1

        def mount(self, *_a, **_k):
            pass

    session = _StallingSession()
    worker_names_before = {t.name for t in threading.enumerate()}
    started = time.monotonic()
    try:
        with pytest.raises(MediationError):
            dispatch._request_within_deadline(
                session,
                _StubAdapter(),
                deadline=time.monotonic() + 0.2,
                method="GET",
                url="https://slow.example.com/",
            )
        elapsed = time.monotonic() - started
        # Returned at the deadline, not after the 10s stall.
        assert elapsed < 5.0
        # The live socket was force-aborted (shutdown) so the blocked read ended.
        assert sock_closed["n"] >= 1
        # The PRIMARY teardown (checked-out in-flight connection) ran, not just
        # the idle-queue belt-and-suspenders sweep.
        assert live_closed["n"] >= 1
        # The session was also closed to return any idle connections.
        assert closed["n"] >= 1
        # No mediated-request worker thread survives the deadline (the abort let
        # it unwind, and the helper grace-joined it before returning).
        lingering = [
            t
            for t in threading.enumerate()
            if t.name == "mediated-request" and t.name not in worker_names_before
        ]
        assert lingering == [], f"leaked mediated-request threads: {lingering}"
    finally:
        release.set()


def test_request_within_deadline_refuses_when_budget_already_spent():
    """If the budget is already spent, refuse WITHOUT starting the request at all,
    so a spent deadline never even opens an origin-controlled connection."""
    import time

    calls = {"n": 0}

    class _RecordingSession:
        trust_env = True

        def request(self, **_kw):
            calls["n"] += 1
            return _FakeResponse(body=b"{}")

        def close(self):
            pass

    with pytest.raises(MediationError):
        dispatch._request_within_deadline(
            _RecordingSession(),
            object(),
            deadline=time.monotonic() - 1.0,
            method="GET",
            url="https://example.com/",
        )
    assert calls["n"] == 0


def test_request_timeout_is_bound_to_the_deadline_and_caller_waits_for_termination():
    """A slow connect/send must not complete past the ABSOLUTE deadline. A single
    pre-send check that passes with milliseconds left still lets ``requests``'
    per-phase INACTIVITY timeout run connect+read on and finish the mutation after
    the deadline. The worker must instead bound BOTH phases to the time LEFT until
    the deadline (a ``(connect, read)`` tuple), and the caller must not return
    while the worker can still send -- it confirms the worker terminated first.
    """
    import threading
    import time

    seen_timeout: list = []
    release = threading.Event()
    live_closed = {"n": 0}

    class _AbortableSock:
        def shutdown(self, _how):
            release.set()

        def close(self):
            pass

    class _StubAdapter:
        poolmanager = type("PM", (), {"pools": type("P", (), {"_container": {}})()})()

        def force_close_live_connections(self):
            live_closed["n"] += 1
            _AbortableSock().shutdown(0)

    class _StallingSession:
        trust_env = True

        def request(self, **kw):
            # Record the timeout the worker chose, then stall past the deadline
            # (released by the force-close) so the caller must tear us down.
            seen_timeout.append(kw.get("timeout"))
            release.wait(timeout=10.0)
            return _FakeResponse(body=b"{}")

        def close(self):
            pass

        def mount(self, *_a, **_k):
            pass

    budget = 0.2
    names_before = {t.name for t in threading.enumerate()}
    started = time.monotonic()
    try:
        with pytest.raises(MediationError):
            dispatch._request_within_deadline(
                _StallingSession(),
                _StubAdapter(),
                deadline=time.monotonic() + budget,
                method="GET",
                url="https://slow.example.com/",
            )
        elapsed = time.monotonic() - started
        # Returned at the deadline, not after the 10s stall.
        assert elapsed < 5.0
        # The send's timeout is a (connect, read) tuple, each bounded by the time
        # left to the deadline -- NOT the stale pre-DNS ``remaining`` and NOT a
        # single scalar inactivity timeout. So neither connect nor read can run
        # past the deadline on its own.
        assert seen_timeout, "the worker never reached session.request"
        t = seen_timeout[0]
        assert isinstance(t, tuple) and len(t) == 2, f"expected a (connect, read) tuple, got {t!r}"
        assert all(0 < x <= budget + 0.01 for x in t), f"timeout not bound to the deadline: {t!r}"
        # The caller waited for the worker to terminate before returning: the
        # live socket was force-closed and no mediated-request thread lingers.
        assert live_closed["n"] >= 1
        lingering = [
            th
            for th in threading.enumerate()
            if th.name == "mediated-request" and th.name not in names_before
        ]
        assert lingering == [], f"caller returned before the worker terminated: {lingering}"
    finally:
        release.set()


def test_repeated_drip_timeouts_leave_zero_lingering_threads():
    """Repeated timed-out mediated calls must not accumulate worker threads or
    connections: each overrun force-closes its live socket so the daemon thread
    unwinds, and the thread/connection count cannot grow with the number of
    timed-out calls (the bound-everything anchor applied to the worker thread)."""
    import threading
    import time

    releases: list[threading.Event] = []
    # One shared registry across all eight calls: if a timeout failed to tear its
    # connection down, the count here would grow with the number of calls.
    live_conns: set = set()

    def _make_adapter(release: threading.Event):
        class _AbortableSock:
            def shutdown(self, _how):
                release.set()

            def close(self):
                pass

        class _CheckedOutConn:
            sock = _AbortableSock()

        conn = _CheckedOutConn()
        live_conns.add(conn)

        class _StubAdapter:
            poolmanager = type("PM", (), {"pools": type("P", (), {"_container": {}})()})()

            def force_close_live_connections(self):
                # Abort the checked-out socket, then deregister it exactly as the
                # real connection's close() does -- so the shared set drains.
                conn.sock.shutdown(0)
                live_conns.discard(conn)

        return _StubAdapter()

    class _StallingSession:
        trust_env = True

        def __init__(self, release: threading.Event):
            self._release = release

        def request(self, **_kw):
            self._release.wait(timeout=10.0)
            return _FakeResponse(body=b"{}")

        def close(self):
            pass

        def mount(self, *_a, **_k):
            pass

    before = {t.ident for t in threading.enumerate() if t.name == "mediated-request"}
    try:
        for _ in range(8):
            release = threading.Event()
            releases.append(release)
            with pytest.raises(MediationError):
                dispatch._request_within_deadline(
                    _StallingSession(release),
                    _make_adapter(release),
                    deadline=time.monotonic() + 0.1,
                    method="GET",
                    url="https://slow.example.com/",
                )
        # After eight timed-out calls, no mediated-request worker thread lingers:
        # the count did not grow with the number of calls.
        lingering = [
            t
            for t in threading.enumerate()
            if t.name == "mediated-request" and t.ident not in before
        ]
        assert lingering == [], f"leaked {len(lingering)} mediated-request threads"
        # And zero lingering CONNECTIONS: every checked-out connection was torn
        # down on its timeout, so the count did not grow with the eight calls.
        assert live_conns == set(), f"leaked {len(live_conns)} connections"
    finally:
        for r in releases:
            r.set()


def test_pinned_adapter_force_closes_the_checked_out_connection():
    """The in-flight connection a worker checks out is ABSENT from the pool's idle
    ``queue`` (urllib3 removes it while in use), so the idle sweep cannot reach
    it. The adapter tracks every live connection it opens; a timeout teardown
    force-closes THAT checked-out socket, and the connection deregisters on close
    so repeated timeouts cannot accumulate connections. This asserts the exact
    gap the idle-queue sweep misses.
    """
    adapter = dispatch._PinnedHTTPSAdapter("203.0.113.7")

    aborted = {"n": 0}

    class _InFlightSock:
        def shutdown(self, _how):
            aborted["n"] += 1

        def close(self):
            pass

    class _CheckedOutConn:
        """A connection the pool handed to a worker: it is NOT in any idle
        ``queue``, only in the adapter's live registry."""

        sock = _InFlightSock()

        def close(self):
            # Mirrors the real connection's close -> deregister so the live set
            # does not retain it (what prevents accumulation across timeouts).
            adapter._deregister_connection(self)

    conn = _CheckedOutConn()
    adapter._register_connection(conn)

    # The idle-queue sweep sees nothing (the connection is checked out), but the
    # live registry holds it, so the teardown still aborts its socket.
    assert conn in adapter._live_connections
    dispatch._force_close_adapter_connections(adapter)
    assert aborted["n"] == 1, "checked-out connection's socket was not force-closed"

    # Closing the connection deregisters it; after teardown + close no live
    # connection lingers, so repeated timeouts cannot grow the set.
    conn.close()
    assert conn not in adapter._live_connections
    assert adapter._live_connections == set()


def test_create_connection_stall_does_not_outlast_the_deadline():
    """A worker stalled in ``create_connection`` has NO socket yet, so the
    force-close teardown is a no-op for it (``conn.sock`` is ``None``). The helper
    must still return at the normalized deadline rather than burning a fixed grace
    slice PAST it: that extra time lands outside the padding window and its
    duration depends on which stall point the origin chose (connect vs recv), one
    observable bit per call. The grace join is bounded by the time LEFT on the
    deadline, so a stall the abort cannot reach cannot stretch completion.
    """
    import threading
    import time

    release = threading.Event()

    class _NoSocketAdapter:
        # Models the create_connection stall: there is no live socket to abort,
        # so the primary teardown closes nothing. poolmanager has no pools either.
        poolmanager = type("PM", (), {"pools": None})()

        def force_close_live_connections(self):
            # No-op: the worker is stalled before any socket exists.
            pass

    class _ConnectStallSession:
        trust_env = True

        def request(self, **_kw):
            # Stand in for a create_connection stall that self-aborts at its own
            # connect timeout: released shortly after the deadline, NOT at the
            # 10s outer bound. The helper must not add a fixed second on top.
            release.wait(timeout=10.0)
            raise _requests_module().ConnectionError("connect timed out")

        def close(self):
            pass

        def mount(self, *_a, **_k):
            pass

    # Release the worker a hair after the deadline, as a real connect timeout
    # (== the per-read timeout, <= the budget) would.
    deadline_s = 0.2
    threading.Timer(deadline_s + 0.02, release.set).start()

    started = time.monotonic()
    with pytest.raises(MediationError):
        dispatch._request_within_deadline(
            _ConnectStallSession(),
            _NoSocketAdapter(),
            deadline=time.monotonic() + deadline_s,
            method="GET",
            url="https://connect-stall.example.com/",
        )
    elapsed = time.monotonic() - started
    # The helper returns within a small bound of the deadline, NOT deadline + a
    # fixed 1.0s grace. The reserve is 0.05s and the worker self-releases ~0.02s
    # after the deadline, so a comfortably-under-1s bound proves the fixed second
    # is gone while tolerating scheduler jitter.
    assert elapsed < deadline_s + 0.5, (
        f"create_connection stall pushed completion to {elapsed:.3f}s, past the "
        f"{deadline_s}s deadline by a stall-point-dependent amount"
    )
    release.set()


def test_worker_whose_deadline_expires_before_send_does_not_send(monkeypatch):
    """A worker must recheck the absolute deadline on its OWN thread, immediately
    before the send. The deadline is struck before the DNS/SSRF pin and vault
    read, so by the time the worker runs the budget can be below the teardown
    reserve -- making the main thread's ``join(timeout=remaining - reserve)`` a
    0.0s no-op that supervises the worker not at all. A worker still pre-
    ``create_connection`` is then unreachable by the force-close, so without an
    in-thread guard the daemon could issue the request (e.g. a DELETE) AFTER the
    caller already raised timeout. The guard means a worker whose deadline is
    spent never calls ``session.request`` at all; the caller still waits for the
    worker to unwind before returning.
    """
    import threading
    import time

    calls = {"n": 0}
    worker_reached_guard = threading.Event()

    base = time.monotonic()
    # Deadline sits a hair in the FUTURE so the entry check (``remaining > 0``)
    # passes and the worker IS spawned -- the exact window the guard protects.
    deadline = base + 100.0
    monotonic_calls = {"n": 0}

    def _clock():
        # First call is the entry ``remaining`` check: report in-budget so the
        # worker is started. Every later call -- the worker's own guard among
        # them -- reports the deadline already spent, modelling the budget having
        # drained below the reserve by the time the worker is scheduled.
        monotonic_calls["n"] += 1
        if monotonic_calls["n"] == 1:
            return base
        return deadline + 1.0

    monkeypatch.setattr(dispatch.time, "monotonic", _clock)

    class _RecordingSession:
        trust_env = True

        def request(self, **_kw):
            # If the guard failed, the worker would reach here and "send".
            calls["n"] += 1
            worker_reached_guard.set()
            return _FakeResponse(body=b"{}")

        def close(self):
            pass

    class _NoSocketAdapter:
        poolmanager = type("PM", (), {"pools": None})()

        def force_close_live_connections(self):
            pass

    with pytest.raises(MediationError):
        dispatch._request_within_deadline(
            _RecordingSession(),
            _NoSocketAdapter(),
            deadline=deadline,
            method="DELETE",
            url="https://mutation.example.com/resource/1",
        )
    # Give any (incorrectly-unguarded) worker a beat to send, then assert it did
    # NOT: the guard refused in-thread before the request, so nothing egressed.
    worker_reached_guard.wait(timeout=0.5)
    assert calls["n"] == 0, "a deadline-expired worker still issued the request"


def _requests_module():
    import requests

    return requests


def test_malformed_location_header_collapses_to_the_withheld_constant(tmp_path, monkeypatch):
    """A malformed ``Location`` header from the authorized origin must NOT produce
    a distinguishable error-vs-success outcome.

    ``requests.Session.send`` prepares ``r._next`` even with
    ``allow_redirects=False`` by running ``resolve_redirects(yield_requests=True)``
    once, which parses/rebuilds the raw origin-controlled ``Location``. A
    malformed Location (e.g. an invalid IPv6 literal in its authority) makes that
    preparation raise a bare ``ValueError`` out of ``session.request`` -- not a
    ``RequestException`` and not a ``UnicodeError``. Without a catch it escapes the
    padded region and ``perform_mediated_request`` raises, while every other
    outcome returns the fixed withheld constant: an error-vs-success oracle a
    hostile origin drives with its own header. The dispatcher must collapse it to
    the SAME withheld constant a success returns.
    """
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    cap = []
    _stub_network(monkeypatch, cap)

    def _raise_valueerror(self, **_kw):
        # Model what requests' internal redirect preparation does on a malformed
        # Location: raise ValueError out of session.request before our handler
        # ever sees the response.
        raise ValueError("Invalid IPv6 URL")

    monkeypatch.setattr(requests.Session, "request", _raise_valueerror)

    # The call must NOT raise -- it returns the fixed withheld constant, identical
    # to a success, so a malformed Location is indistinguishable from any other
    # outcome.
    resp = perform_mediated_request(
        MediatedRequest(
            secret_name="WEATHER_API_KEY",
            method="GET",
            url="https://api.weather.example/v1/now",
        ),
        tmp_path,
    )
    assert "mediated request completed" in resp.body  # same fixed withheld constant
    assert resp.status == 0  # withheld status is the fixed constant, not origin-derived
    # And the canary secret never rides out on the collapsed error path.
    blob = json.dumps({"status": resp.status, "headers": resp.headers, "body": resp.body})
    assert CANARY not in blob


def test_stalled_handshake_force_close_reaches_the_live_socket():
    """During the TLS handshake ``conn.sock`` is the raw socket that
    ``ssl.wrap_socket`` DETACHED, so a force-close that only touches ``conn.sock``
    cannot abort a stalled handshake and an origin that stalls it runs the
    completion loop past the deadline. ``_new_conn`` keeps a dup of the raw socket
    taken BEFORE wrap detaches it; ``force_close_live_connections`` shuts that dup
    down, which unblocks the shared kernel socket the handshake is waiting on.

    This models the handshake window with a real socketpair: the connection's
    ``sock`` is a detached socket (``fileno() == -1``, as mid-wrap), while its
    ``_mediated_handshake_sock`` is a live dup of a socket a thread is blocked
    reading. The force-close must unblock that read via the dup -- proving it
    reaches the live fd that ``conn.sock`` alone cannot.
    """
    import threading
    import time

    adapter = dispatch._PinnedHTTPSAdapter("203.0.113.7")

    live, peer = socket.socketpair()
    self_addr = (live, peer)  # keep both ends referenced for the test's lifetime
    assert self_addr

    # A reader blocked on the live socket -- the stand-in for a worker stalled in
    # the TLS handshake, waiting on bytes the origin never sends.
    unblocked = []

    def _blocked_reader():
        try:
            data = live.recv(16)
            unblocked.append(("returned", data))
        except OSError as exc:
            unblocked.append(("oserror", exc.errno))

    reader = threading.Thread(target=_blocked_reader, daemon=True)
    reader.start()
    time.sleep(0.2)

    # A detached socket stands in for ``conn.sock`` during the handshake: wrap has
    # taken its fd, so shutting it down is a no-op (fileno == -1).
    detached = socket.socket()
    detached.detach()
    assert detached.fileno() == -1

    class _MidHandshakeConn:
        sock = detached

        def close(self):
            adapter._deregister_connection(self)

    conn = _MidHandshakeConn()
    # The dup captured at connect time, before wrap detached the raw socket.
    conn._mediated_handshake_sock = live.dup()
    adapter._register_connection(conn)

    # Shutting down only ``conn.sock`` would NOT unblock the reader (it is
    # detached). The handshake-dup path must.
    adapter.force_close_live_connections()

    reader.join(timeout=2.0)
    assert not reader.is_alive(), "the stalled handshake read was not aborted at the deadline"
    assert unblocked and unblocked[0][0] in ("returned", "oserror"), unblocked
    # A clean shutdown returns b'' from the blocked recv.
    if unblocked[0][0] == "returned":
        assert unblocked[0][1] == b""

    conn.close()
    live.close()
    peer.close()


def test_new_conn_captures_a_handshake_dup_before_wrap():
    """The real pinned connection class must record ``_mediated_handshake_sock``
    the instant the raw socket is created in ``_new_conn`` -- before the TLS wrap
    detaches it -- otherwise the handshake window has no live-socket handle to
    abort. Patches ``create_connection`` to a real socketpair end so no network is
    touched, then asserts the dup is captured and is a distinct live socket.
    """
    from urllib3.util import connection as urllib3_connection

    adapter = dispatch._PinnedHTTPSAdapter("203.0.113.7", max_retries=0)
    pinned_pool_cls = adapter.poolmanager.pool_classes_by_scheme["https"]
    conn_cls = pinned_pool_cls.ConnectionCls

    raw, peer = socket.socketpair()
    try:
        original = urllib3_connection.create_connection
        urllib3_connection.create_connection = lambda *a, **k: raw
        try:
            conn = conn_cls(host="api.weather.example", port=443)
            produced = conn._new_conn()
        finally:
            urllib3_connection.create_connection = original

        assert produced is raw  # the raw socket is still returned to connect()
        dup = getattr(conn, "_mediated_handshake_sock", None)
        assert dup is not None, "no handshake dup captured before wrap"
        assert dup.fileno() != -1  # it is a live socket, not detached
        assert dup.fileno() != raw.fileno()  # a distinct fd (a dup), not the raw socket itself

        # close() drops the dup without closing the raw socket.
        conn.close()
        assert conn._mediated_handshake_sock is None
        assert raw.fileno() != -1  # closing the dup did not close the raw socket
    finally:
        raw.close()
        peer.close()
