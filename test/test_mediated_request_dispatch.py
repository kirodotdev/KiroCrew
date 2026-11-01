"""Tests for the trusted mediated-request dispatcher.

The load-bearing property under test: the secret's PLAINTEXT is used only to
build the outbound request and never appears in the tool result or an error. A
canary value is stored and asserted absent from every observable surface.

The network is a fake ``aiohttp.ClientSession`` installed on the dispatcher, and
DNS is the dispatcher's ``_resolve`` hook, so the REAL shared vet
(``link_unfurl.vet_unfurl_url``) runs on every hop without touching the network.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew import link_unfurl
from kiro_crew.secrets import SecretVault
from kiro_crew.secrets_mediation import dispatch
from kiro_crew.secrets_mediation.dispatch import (
    MediatedRequest,
    MediationError,
    perform_mediated_request,
)
from kiro_crew.secrets_mediation.policy import PolicyError, policy_path
from kiro_crew.testing.clock import ManualClock

CANARY = "sk-canary-3f9a2b7c1d8e-DO-NOT-LEAK"
OPAQUE_CREDENTIAL = "opaque-7a9c2d4e6f8b1a3c5e7d9f0b2a4c6e8f"
ORIGIN = "https://api.weather.example"
PUBLIC_IP = "93.184.216.34"
#: The real pad, captured before the autouse fixture stubs it.
_REAL_PAD = dispatch._wait_for_withheld_deadline


@pytest.fixture(autouse=True)
def _host_policy_key(tmp_path, _floor_monkeypatch):
    """Pin the token-signing secret, resolve every host to a public address, skip the pad."""
    from kiro_crew.secrets_mediation import provenance

    _floor_monkeypatch.setattr(provenance, "_token_root", lambda: b"T" * 32)
    _floor_monkeypatch.setattr(dispatch, "_resolve", lambda host, port: [PUBLIC_IP])

    async def _no_pad(_deadline: float) -> None:
        return None

    _floor_monkeypatch.setattr(dispatch, "_wait_for_withheld_deadline", _no_pad)


def _seed_vault(config_dir, value=CANARY):
    SecretVault(config_dir).set_sync("WEATHER_API_KEY", value)


def _write_policy(config_dir, authorizations):
    from kiro_crew.secrets_mediation.provenance import SIGNATURE_FIELD, sign_secret_authorizations

    sig = sign_secret_authorizations(authorizations)
    path = policy_path(config_dir)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "authorizations": authorizations, SIGNATURE_FIELD: sig}),
        encoding="utf-8",
    )


def _authorize(config_dir, origin=ORIGIN, placement=None):
    entry = {"origin": origin, "placement": placement or {"type": "bearer"}}
    _write_policy(config_dir, {"WEATHER_API_KEY": entry})


class _FakeResponse:
    def __init__(self, status: int, headers: dict[str, str]) -> None:
        self.status = status
        self.headers = headers
        self.body_read = False

    async def read(self) -> bytes:  # never called by the dispatcher
        self.body_read = True
        return b"secret-echo"

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeNetwork:
    """Records every send; answers from a list of (status, headers), last repeats."""

    def __init__(self, monkeypatch, replies=None, stall: bool = False) -> None:
        self.sends: list[dict] = []
        self.cancelled = 0
        self.connectors: list = []
        self.responses: list[_FakeResponse] = []
        self._replies = list(replies or [(200, {"X-Echo": CANARY})])
        self._stall = stall
        network = self

        class _Session:
            def __init__(self, *, connector, cookie_jar, trust_env, auto_decompress):
                network.connectors.append(connector)
                assert isinstance(cookie_jar, dispatch.aiohttp.DummyCookieJar)
                assert trust_env is False

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            def request(self, method, url, *, params, headers, json, allow_redirects):
                assert allow_redirects is False
                return network._respond(method, url, params, headers, json)

        monkeypatch.setattr(dispatch.aiohttp, "ClientSession", _Session)
        monkeypatch.setattr(
            dispatch, "pinned_connector", lambda vetted: ("pinned", vetted.host, vetted.addresses)
        )

    def _respond(self, method, url, params, headers, body):
        network = self

        class _Ctx:
            async def __aenter__(self):
                network.sends.append(
                    {
                        "method": method,
                        "url": url,
                        "params": params,
                        "headers": dict(headers),
                        "json": body,
                    }
                )
                if network._stall:
                    # An origin that never answers: only the deadline ends it.
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        network.cancelled += 1
                        raise
                index = min(len(network.sends) - 1, len(network._replies) - 1)
                status, hdrs = network._replies[index]
                resp = _FakeResponse(status, hdrs)
                network.responses.append(resp)
                return resp

            async def __aexit__(self, *exc):
                return None

        return _Ctx()


def _req(**overrides) -> MediatedRequest:
    fields = {"secret_name": "WEATHER_API_KEY", "method": "POST", "url": f"{ORIGIN}/v1/alerts"}
    fields.update(overrides)
    return MediatedRequest(**fields)


def _no_secret_read(monkeypatch):
    monkeypatch.setattr(SecretVault, "get", lambda self, name: pytest.fail("secret was read"))


def _assert_withheld(resp) -> None:
    assert resp == dispatch._withheld_response(ORIGIN)
    assert CANARY not in json.dumps(resp.__dict__)


@pytest.mark.asyncio
async def test_bearer_secret_injected_but_absent_from_result(tmp_path, monkeypatch):
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    net = _FakeNetwork(monkeypatch)
    resp = await perform_mediated_request(_req(json_body={"city": "Seattle"}), tmp_path)
    assert net.sends[0]["headers"]["Authorization"] == f"Bearer {CANARY}"
    assert net.sends[0]["json"] == {"city": "Seattle"}
    # The body is never read and nothing the origin sent comes back.
    assert net.responses[0].body_read is False
    _assert_withheld(resp)


@pytest.mark.asyncio
async def test_header_placement_injects_named_header(tmp_path, monkeypatch):
    _seed_vault(tmp_path)
    _authorize(tmp_path, placement={"type": "header", "header": "X-Api-Key"})
    net = _FakeNetwork(monkeypatch)
    await perform_mediated_request(_req(headers={"x-api-key": "agent-chosen"}), tmp_path)
    sent = net.sends[0]["headers"]
    assert sent["X-Api-Key"] == CANARY
    # A caller header that collides with the injection slot is dropped.
    assert "x-api-key" not in sent
    assert "Authorization" not in sent


@pytest.mark.asyncio
async def test_send_goes_through_the_shared_vet_and_pin(tmp_path, monkeypatch):
    """The dispatcher vets with link_unfurl.vet_unfurl_url and connects through
    pinned_connector, so it cannot drift from the product's other fetchers."""
    _seed_vault(tmp_path)
    _authorize(tmp_path, origin="https://api.weather.example:8443")
    seen: list = []
    real_vet = link_unfurl.vet_unfurl_url

    def _spy(url, **kwargs):
        seen.append((url, kwargs.get("allow_any_port"), kwargs.get("allow_private", False)))
        return real_vet(url, **kwargs)

    monkeypatch.setattr(dispatch, "vet_unfurl_url", _spy)
    net = _FakeNetwork(monkeypatch)
    await perform_mediated_request(_req(url="https://api.weather.example:8443/x"), tmp_path)
    # An owner-authorized explicit port is allowed; private targets are not.
    assert seen == [("https://api.weather.example:8443/x", True, False)]
    assert net.connectors == [("pinned", "api.weather.example", (PUBLIC_IP,))]


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1"])
async def test_vet_refusal_never_reads_the_secret_or_sends(tmp_path, monkeypatch, address):
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    monkeypatch.setattr(dispatch, "_resolve", lambda host, port: [address])
    _no_secret_read(monkeypatch)
    net = _FakeNetwork(monkeypatch)
    resp = await perform_mediated_request(_req(), tmp_path)
    assert net.sends == []
    _assert_withheld(resp)


@pytest.mark.asyncio
async def test_a_mixed_public_and_private_answer_is_refused(tmp_path, monkeypatch):
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    monkeypatch.setattr(dispatch, "_resolve", lambda host, port: [PUBLIC_IP, "10.1.2.3"])
    net = _FakeNetwork(monkeypatch)
    _assert_withheld(await perform_mediated_request(_req(), tmp_path))
    assert net.sends == []


@pytest.mark.asyncio
async def test_corrupt_vault_read_collapses_to_the_withheld_constant(tmp_path, monkeypatch):
    from cryptography.exceptions import InvalidTag

    _authorize(tmp_path)
    net = _FakeNetwork(monkeypatch)
    for exc in (ValueError("corrupt store record"), InvalidTag(), OSError("vault unreadable")):

        def _raise(self, name, _e=exc):
            raise _e

        monkeypatch.setattr(SecretVault, "get", _raise)
        _assert_withheld(await perform_mediated_request(_req(), tmp_path))
    assert net.sends == []


@pytest.mark.asyncio
async def test_authorized_but_unstored_secret_collapses(tmp_path, monkeypatch):
    _authorize(tmp_path)
    net = _FakeNetwork(monkeypatch)
    _assert_withheld(await perform_mediated_request(_req(), tmp_path))
    assert net.sends == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_fields", "error"),
    [
        ({"url": "https://evil.example.com/steal"}, PolicyError),
        ({"url": "https://user@api.weather.example/x"}, MediationError),
        ({"url": "https://api.weather.example\\@evil.example/x"}, MediationError),
        ({"url": "https://[::1/x"}, MediationError),
        ({"method": "GET"}, MediationError),
        ({"timeout_s": float("nan")}, MediationError),
        ({"json_body": {"note": "my key is AKIAIOSFODNN7EXAMPLE"}}, MediationError),
        ({"url": f"{ORIGIN}/x?token=AKIAIOSFODNN7EXAMPLE"}, MediationError),
        ({"json_body": {"blob": "x" * 300_000}}, MediationError),
    ],
)
async def test_pre_send_refusals_raise_before_the_secret_is_read(
    tmp_path, monkeypatch, request_fields, error
):
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    _no_secret_read(monkeypatch)
    net = _FakeNetwork(monkeypatch)
    with pytest.raises(error) as excinfo:
        await perform_mediated_request(_req(**request_fields), tmp_path)
    assert CANARY not in str(excinfo.value)
    assert net.sends == []


@pytest.mark.parametrize(
    ("request_fields", "contextual_text"),
    [
        (
            {"json_body": {"Authorization": f"Bearer {OPAQUE_CREDENTIAL}"}},
            json.dumps({"Authorization": f"Bearer {OPAQUE_CREDENTIAL}"}),
        ),
        (
            {"headers": {"X-Authorization": f"Bearer {OPAQUE_CREDENTIAL}"}},
            f"X-Authorization: Bearer {OPAQUE_CREDENTIAL}",
        ),
        (
            {"query": {"token": OPAQUE_CREDENTIAL}},
            f"{ORIGIN}/v1/alerts?token={OPAQUE_CREDENTIAL}",
        ),
    ],
    ids=("serialized-json", "joined-header", "assembled-query-url"),
)
def test_contextual_outbound_credentials_are_refused(tmp_path, request_fields, contextual_text):
    leaves = [
        text for value in request_fields.values() for text in dispatch._iter_outbound_strings(value)
    ]
    assert all(dispatch.redact_credentials(text)[0] == text for text in leaves)
    assert dispatch.redact_credentials(contextual_text)[0] != contextual_text

    _authorize(tmp_path)
    with pytest.raises(MediationError, match="credential in an agent-supplied value"):
        dispatch.prepare_mediated_request(_req(**request_fields), tmp_path)


def test_clean_contextual_outbound_payload_passes(tmp_path):
    _authorize(tmp_path)
    prepared = dispatch.prepare_mediated_request(
        _req(
            json_body={"operation": "forecast"},
            headers={"X-Request-Reason": "weather-alert"},
            query={"city": "Seattle"},
        ),
        tmp_path,
    )
    assert prepared.caller_headers == {"X-Request-Reason": "weather-alert"}


@pytest.mark.asyncio
async def test_no_authorization_fails_closed(tmp_path, monkeypatch):
    _seed_vault(tmp_path)
    net = _FakeNetwork(monkeypatch)
    with pytest.raises(PolicyError):
        await perform_mediated_request(_req(), tmp_path)
    assert net.sends == []


@pytest.mark.asyncio
async def test_same_origin_redirect_does_not_replay_the_write(tmp_path, monkeypatch):
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    net = _FakeNetwork(monkeypatch, replies=[(302, {"Location": "/v1/done"}), (200, {})])
    await perform_mediated_request(_req(json_body={"a": 1}, query={"q": "1"}), tmp_path)
    assert [s["method"] for s in net.sends] == ["POST", "GET"]
    assert net.sends[1]["url"] == f"{ORIGIN}/v1/done"
    assert net.sends[1]["json"] is None
    assert net.sends[1]["params"] is None  # the query rode the first hop only


@pytest.mark.asyncio
async def test_307_keeps_method_and_body(tmp_path, monkeypatch):
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    net = _FakeNetwork(monkeypatch, replies=[(307, {"Location": "/v2"}), (200, {})])
    await perform_mediated_request(_req(json_body={"a": 1}), tmp_path)
    assert [(s["method"], s["json"]) for s in net.sends] == [("POST", {"a": 1}), ("POST", {"a": 1})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    ["https://evil.example/x", "http://api.weather.example/x", "https://[::1/x"],
)
async def test_redirect_off_the_authorized_origin_is_not_followed(tmp_path, monkeypatch, location):
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    net = _FakeNetwork(monkeypatch, replies=[(302, {"Location": location})])
    _assert_withheld(await perform_mediated_request(_req(), tmp_path))
    assert len(net.sends) == 1


@pytest.mark.asyncio
async def test_redirect_hop_is_vetted_again(tmp_path, monkeypatch):
    """A same-origin redirect whose host now resolves privately is not followed."""
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    answers = iter([[PUBLIC_IP], ["10.0.0.9"]])
    monkeypatch.setattr(dispatch, "_resolve", lambda host, port: next(answers))
    net = _FakeNetwork(monkeypatch, replies=[(302, {"Location": "/again"}), (200, {})])
    _assert_withheld(await perform_mediated_request(_req(), tmp_path))
    assert len(net.sends) == 1


@pytest.mark.asyncio
async def test_redirect_limit_is_enforced(tmp_path, monkeypatch):
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    net = _FakeNetwork(monkeypatch, replies=[(302, {"Location": "/loop"})])
    _assert_withheld(await perform_mediated_request(_req(), tmp_path))
    assert len(net.sends) == dispatch._MAX_REDIRECTS + 1


@pytest.mark.asyncio
async def test_a_stalled_origin_is_cut_off_at_the_deadline(tmp_path, monkeypatch):
    """The whole send is one task under wait_for: a stall is cancelled at the
    deadline, so it neither outlives the budget nor returns anything different."""
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    net = _FakeNetwork(monkeypatch, stall=True)
    budgets: list[float] = []
    real_wait_for = asyncio.wait_for

    async def _spy_wait_for(aw, timeout):
        budgets.append(timeout)
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(dispatch, "asyncio", _AsyncioWith(wait_for=_spy_wait_for))
    resp = await perform_mediated_request(_req(timeout_s=0.3), tmp_path)
    # The send ran under the request's own budget and was cancelled by it.
    assert budgets == [0.3]
    assert len(net.sends) == 1 and net.cancelled == 1
    _assert_withheld(resp)


class _AsyncioWith:
    """``dispatch``'s own ``asyncio`` binding with some names replaced.

    Only the module under test sees the replacements; the real ``asyncio`` module,
    and so the event loop and every other caller in the worker, is untouched.
    """

    def __init__(self, **overrides) -> None:
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(asyncio, name)


@pytest.mark.asyncio
async def test_completion_is_padded_to_the_request_deadline(tmp_path, monkeypatch):
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    clock = ManualClock()
    clock.install(monkeypatch, dispatch)
    pads: list[float] = []

    async def _record(deadline: float) -> None:
        pads.append(deadline - dispatch.time.monotonic())

    monkeypatch.setattr(dispatch, "_wait_for_withheld_deadline", _record)
    _FakeNetwork(monkeypatch)
    await perform_mediated_request(_req(timeout_s=5.0), tmp_path)
    # Called once, and the clock did not move, so the whole 5 s budget is left.
    assert pads == [5.0]


@pytest.mark.asyncio
async def test_real_pad_waits_out_the_remaining_budget(monkeypatch):
    # The autouse fixture stubs the module attribute; this is the real body,
    # captured at import time. Both clock and sleep are dispatch's own bindings.
    clock = ManualClock()
    clock.install(monkeypatch, dispatch)
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(dispatch, "asyncio", _AsyncioWith(sleep=_sleep))
    now = dispatch.time.monotonic()
    await _REAL_PAD(now + 0.25)
    assert slept == [0.25]
    # A deadline already in the past returns at once, without sleeping.
    await _REAL_PAD(now - 1.0)
    assert slept == [0.25]


@pytest.mark.asyncio
async def test_success_and_every_post_send_failure_are_byte_identical(tmp_path, monkeypatch):
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    _FakeNetwork(monkeypatch, replies=[(500, {"X-Echo": CANARY})])
    failure = await perform_mediated_request(_req(), tmp_path)
    _FakeNetwork(monkeypatch, replies=[(200, {})])
    success = await perform_mediated_request(_req(), tmp_path)
    monkeypatch.setattr(dispatch, "_resolve", lambda host, port: ["10.0.0.1"])
    refused = await perform_mediated_request(_req(), tmp_path)
    assert failure == success == refused


@pytest.mark.asyncio
async def test_a_transport_error_collapses_without_leaking(tmp_path, monkeypatch):
    _seed_vault(tmp_path)
    _authorize(tmp_path)
    net = _FakeNetwork(monkeypatch)

    def _boom(*args, **kwargs):
        raise dispatch.aiohttp.ClientConnectionError(f"reset while sending {CANARY}")

    monkeypatch.setattr(net, "_respond", _boom)
    _assert_withheld(await perform_mediated_request(_req(), tmp_path))


@pytest.mark.asyncio
async def test_unencodable_secret_collapses_without_leaking(tmp_path, monkeypatch):
    _seed_vault(tmp_path, value="caf\u00e9-\u2603")
    _authorize(tmp_path)
    net = _FakeNetwork(monkeypatch)

    def _encode_fail(*args, **kwargs):
        raise UnicodeEncodeError("latin-1", "\u2603", 0, 1, "ordinal not in range")

    monkeypatch.setattr(net, "_respond", _encode_fail)
    resp = await perform_mediated_request(_req(), tmp_path)
    assert "\u2603" not in json.dumps(resp.__dict__)
    _assert_withheld(resp)


@pytest.mark.asyncio
async def test_blocking_steps_run_off_the_loop(tmp_path, monkeypatch):
    import threading

    _seed_vault(tmp_path)
    _authorize(tmp_path)
    _FakeNetwork(monkeypatch)
    loop_thread = threading.get_ident()
    threads: dict[str, int] = {}
    real_prepare = dispatch.prepare_mediated_request
    real_read = dispatch._read_secret

    def _prepare(*args):
        threads["prepare"] = threading.get_ident()
        return real_prepare(*args)

    def _read(*args):
        threads["read"] = threading.get_ident()
        return real_read(*args)

    def _resolve(host, port):
        threads["resolve"] = threading.get_ident()
        return [PUBLIC_IP]

    monkeypatch.setattr(dispatch, "prepare_mediated_request", _prepare)
    monkeypatch.setattr(dispatch, "_read_secret", _read)
    monkeypatch.setattr(dispatch, "_resolve", _resolve)
    await perform_mediated_request(_req(), tmp_path)
    assert set(threads) == {"prepare", "read", "resolve"}
    assert loop_thread not in threads.values()


def test_the_module_ships_no_second_vet_or_transport():
    """One fetch vet in the product: no requests/urllib3 transport here."""
    import inspect

    source = inspect.getsource(dispatch)
    for name in ("import requests", "import urllib3", "from urllib3", "getaddrinfo"):
        assert name not in source
