"""``impl_sagemaker``: the ``sagemaker://`` endpoint, InvokeEndpoint, retries, errors.

The success and error paths run a REAL boto3 ``sagemaker-runtime`` client behind
botocore's ``Stubber``, so the parameters are checked against SageMaker's own
service model and the errors arrive in the shape botocore really raises. Nothing
reaches the network: every client a test builds refuses to send.
"""

from __future__ import annotations

import asyncio
import functools
import io
import json
import logging
from types import SimpleNamespace

import pytest

botocore = pytest.importorskip("botocore")
boto3 = pytest.importorskip("boto3")

from botocore.exceptions import NoCredentialsError, ReadTimeoutError  # noqa: E402
from botocore.response import StreamingBody  # noqa: E402
from botocore.stub import Stubber  # noqa: E402

from kiro_crew.config.sections import DecisionProviderConfig  # noqa: E402
from kiro_crew.decisions import capability, gate, impl_sagemaker  # noqa: E402
from kiro_crew.decisions.impl_jev import JevOracle  # noqa: E402
from kiro_crew.decisions.impl_sagemaker import (  # noqa: E402
    IMAGES_ATTRIBUTE,
    MAX_ATTEMPTS,
    BoxAnswer,
    SageMakerConfigError,
    SageMakerCredentialsError,
    SageMakerEndpointNotFound,
    SageMakerModelError,
    SageMakerOracle,
    SageMakerProtocolError,
    SageMakerTarget,
    SageMakerThrottled,
    SageMakerTimeout,
    SageMakerUnavailable,
    box_answers,
    image_asset,
    invoke,
    invoke_sync,
    is_sagemaker_endpoint,
    parse_box,
    parse_endpoint,
)
from kiro_crew.decisions.types import Choice, Noul  # noqa: E402

ENDPOINT = "sagemaker://argus-9b?region=us-west-2"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
TARGET = SageMakerTarget("argus-9b", "us-west-2")
STATE_MARK = "STATEMARK-do-not-log"
QUESTION = [Noul(id="q", prompt="Is this risky?")]
ANSWER = {"answers": {"q": {"type": "noul", "noul": 0.25}}}


class _SendRefused(AssertionError):
    pass


def _refuse_send(**_kwargs):
    raise _SendRefused("a test client tried to send a request")


@pytest.fixture(autouse=True)
def _no_aws(_floor_monkeypatch, tmp_path):
    """No real AWS or legacy boto config, credentials or metadata, no cached client,
    and no client this module builds can send a request."""
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_ARN",
        "AWS_CREDENTIAL_FILE",
        "AWS_ENDPOINT_URL",
        "AWS_ENDPOINT_URL_SAGEMAKER_RUNTIME",
    ):
        _floor_monkeypatch.delenv(name, raising=False)
    _floor_monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    _floor_monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "aws-credentials"))
    _floor_monkeypatch.setenv("BOTO_CONFIG", str(tmp_path / "boto-config"))
    _floor_monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    real_make = impl_sagemaker._make_client

    def guarded_make(target, read_timeout):
        client = real_make(target, read_timeout)
        client.meta.events.register("before-send", _refuse_send)
        return client

    _floor_monkeypatch.setattr(impl_sagemaker, "_make_client", guarded_make)
    impl_sagemaker._clear_clients()
    yield real_make
    impl_sagemaker._clear_clients()


@pytest.fixture
def stubbed(monkeypatch):
    """A real sagemaker-runtime client, stubbed; every request goes through it."""
    client = boto3.session.Session(region_name="us-west-2").client("sagemaker-runtime")
    stubber = Stubber(client)
    made: list[tuple[SageMakerTarget, float]] = []

    def fake_make(target, read_timeout):
        made.append((target, read_timeout))
        return client

    monkeypatch.setattr(impl_sagemaker, "_make_client", fake_make)
    with stubber:
        yield stubber, made
    stubber.assert_no_pending_responses()


def _body(payload: dict) -> StreamingBody:
    raw = json.dumps(payload).encode()
    return StreamingBody(io.BytesIO(raw), len(raw))


def _provider(endpoint: str = ENDPOINT, timeout_ms: int = 4000) -> DecisionProviderConfig:
    return DecisionProviderConfig(endpoint=endpoint, model="argus-9b", timeout_ms=timeout_ms)


def _ask(endpoint: str = ENDPOINT, timeout_ms: int = 4000):
    oracle = SageMakerOracle(_provider(endpoint, timeout_ms))
    return asyncio.run(oracle.ask({"message": STATE_MARK}, QUESTION))


# --- the address -------------------------------------------------------------


def test_parse_endpoint_reads_name_region_profile():
    assert parse_endpoint(ENDPOINT) == TARGET
    assert parse_endpoint(" sagemaker://Argus-9B-v2?region=eu-central-1&profile=research ") == (
        SageMakerTarget("Argus-9B-v2", "eu-central-1", "research")
    )
    # A profile may contain ``+``, which a form decoder would read as a space.
    assert parse_endpoint(f"{ENDPOINT}&profile=team+ml").profile == "team+ml"


@pytest.mark.parametrize(
    "endpoint",
    [
        "sagemaker://argus-9b",
        "sagemaker://argus-9b?region=US-WEST-2",
        "sagemaker://argus-9b?region=us-west-2&region=us-east-1",
        "sagemaker://argus-9b?region=us-west-2&url=https://example.com",
        "sagemaker://argus-9b/invocations?region=us-west-2",
        "sagemaker://user@argus-9b?region=us-west-2",
        "sagemaker://argus-9b:443?region=us-west-2",
        "sagemaker://-argus?region=us-west-2",
        "sagemaker://argus_9b?region=us-west-2",
        "sagemaker://argus-9b?region=us-west-2&profile=bad/profile",
        "sagemaker://argus-9b?region=us-west-2&profile=-option",
        "sagemaker://argus-9b?region=us-iso-east-1",
        "https://api.typesafe.ai/v1/systemone",
    ],
)
def test_parse_endpoint_refuses_anything_else(endpoint):
    with pytest.raises(SageMakerConfigError):
        parse_endpoint(endpoint)


def test_only_the_sagemaker_scheme_is_recognised():
    assert is_sagemaker_endpoint(ENDPOINT)
    assert not is_sagemaker_endpoint("https://api.typesafe.ai/v1/systemone")
    assert not is_sagemaker_endpoint("http://127.0.0.1:8108/v1/systemone")
    assert not is_sagemaker_endpoint("none")
    assert not is_sagemaker_endpoint(None)


# --- off by default ----------------------------------------------------------


def test_default_provider_is_not_sagemaker():
    provider = DecisionProviderConfig()
    assert not is_sagemaker_endpoint(provider.endpoint)
    assert isinstance(gate._oracle(gate.LANE_JEV, provider), JevOracle)


def test_gate_picks_the_sagemaker_oracle_only_for_a_sagemaker_endpoint():
    assert isinstance(gate._oracle(gate.LANE_JEV, _provider()), SageMakerOracle)


# --- success -----------------------------------------------------------------


def test_oracle_sends_the_systemone_body_and_reads_answers(stubbed):
    stubber, made = stubbed
    questions = [
        Choice(id="tier", prompt="How hard?", options=("simple", "complex")),
        Noul(id="risky", prompt="Is this risky?"),
    ]
    sent = {
        "state": {"message": "hello"},
        "model": "argus-9b",
        "questions": {
            "tier": {
                "type": "choice",
                "instructions": "How hard?",
                "criteria": {"simple": None, "complex": None},
            },
            "risky": {"type": "noul", "instructions": "Is this risky?"},
        },
    }
    stubber.add_response(
        "invoke_endpoint",
        {
            "Body": _body(
                {
                    "model": "argus-9b",
                    "answers": {
                        "tier": {
                            "type": "choice",
                            "choice": "simple",
                            "probabilities": {"simple": 0.8, "complex": 0.2},
                        },
                        "risky": {"type": "noul", "noul": 0.1},
                    },
                    "usage": {"input_tokens": 40, "output_tokens": 2},
                }
            ),
            "ContentType": "application/json",
            "ResponseMetadata": {"RequestId": "req-1", "HTTPStatusCode": 200},
        },
        {
            "EndpointName": "argus-9b",
            "Body": json.dumps(sent, separators=(",", ":")).encode(),
            "ContentType": "application/json",
            "Accept": "application/json",
        },
    )
    oracle = SageMakerOracle(_provider())
    answers = asyncio.run(oracle.ask({"message": "hello"}, questions))
    assert answers["tier"].value == "simple" and answers["tier"].p == 0.8
    assert answers["risky"].value == 0.1
    assert oracle.last_response_meta == {
        "provider_model": "argus-9b",
        "input_tokens": 40,
        "output_tokens": 2,
        "request_id": "req-1",
    }
    assert made == [(TARGET, 4.0)]


def test_images_and_box_travel_with_the_opt_in_and_box_answers_parse(stubbed):
    stubber, _ = stubbed
    body = {
        "model": "argus-9b",
        "state": {"photo": "kitchen-1"},
        "assets": {"kitchen-1": image_asset(PNG, "PNG")},
        "questions": {"mug": {"type": "box", "instructions": "the coffee mug in `photo`"}},
    }
    assert body["assets"]["kitchen-1"]["format"] == "png"
    stubber.add_response(
        "invoke_endpoint",
        {
            "Body": _body(
                {
                    "model": "argus-9b",
                    "answers": {
                        "mug": {
                            "type": "box",
                            "box": {"top": 0.35, "bottom": 0.66, "left": 0.2, "right": 0.5},
                            "confidence": 0.9,
                        }
                    },
                }
            )
        },
        {
            "EndpointName": "argus-9b",
            "Body": json.dumps(body, separators=(",", ":")).encode(),
            "ContentType": "application/json",
            "Accept": "application/json",
            "CustomAttributes": IMAGES_ATTRIBUTE,
        },
    )
    parsed = asyncio.run(invoke(ENDPOINT, body, 4000))
    assert box_answers(parsed) == {
        "mug": BoxAnswer(top=0.35, bottom=0.66, left=0.2, right=0.5, confidence=0.9)
    }


@pytest.mark.parametrize(
    "raw",
    [
        {"type": "noul", "noul": 0.5},
        {"type": "box"},
        {"type": "box", "box": {"top": 0.1, "bottom": 0.2, "left": 0.3}},
        {"type": "box", "box": {"top": 0.5, "bottom": 0.2, "left": 0.1, "right": 0.3}},
        {"type": "box", "box": {"top": 0.1, "bottom": 0.2, "left": 0.4, "right": 0.3}},
        {"type": "box", "box": {"top": -0.1, "bottom": 0.2, "left": 0.1, "right": 0.3}},
        {"type": "box", "box": {"top": True, "bottom": 0.2, "left": 0.1, "right": 0.3}},
        {
            "type": "box",
            "box": {"top": 0.1, "bottom": 0.2, "left": 0.1, "right": 0.3},
            "confidence": 2,
        },
    ],
)
def test_parse_box_refuses_a_bad_box(raw):
    with pytest.raises(SageMakerProtocolError):
        parse_box(raw)


def test_image_asset_refuses_formats_the_contract_does_not_take():
    with pytest.raises(SageMakerConfigError):
        image_asset(PNG, "gif")
    with pytest.raises(SageMakerConfigError):
        image_asset(b"", "png")


# --- errors ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "message", "extra", "expected"),
    [
        (
            "ValidationError",
            "Endpoint argus-9b of account ACCOUNTMARK not found.",
            {},
            SageMakerEndpointNotFound,
        ),
        ("UnrecognizedClientException", "bad token ACCOUNTMARK", {}, SageMakerCredentialsError),
        ("ExpiredTokenException", "expired ACCOUNTMARK", {}, SageMakerCredentialsError),
        (
            "ModelError",
            "Received client error (400) ACCOUNTMARK",
            {"OriginalStatusCode": 400},
            SageMakerModelError,
        ),
    ],
)
def test_service_errors_map_to_clear_errors(stubbed, caplog, code, message, extra, expected):
    stubber, _ = stubbed
    stubber.add_client_error(
        "invoke_endpoint",
        service_error_code=code,
        service_message=message,
        http_status_code=400,
        modeled_fields=extra or None,
    )
    caplog.set_level(logging.DEBUG)
    with pytest.raises(expected) as info:
        _ask()
    asset = image_asset(PNG, "png")
    stubber.add_client_error(
        "invoke_endpoint",
        service_error_code=code,
        service_message=message,
        http_status_code=400,
        modeled_fields=extra or None,
    )
    with pytest.raises(expected) as image_info:
        asyncio.run(invoke(ENDPOINT, {"state": "photo", "assets": {"photo": asset}}))
    assert asset["data_base64"] not in str(image_info.value) + caplog.text
    if expected is SageMakerModelError:
        assert info.value.status == 400
    # Neither the conversation nor the service's own message is repeated.
    text = str(info.value) + caplog.text
    assert STATE_MARK not in text
    assert "ACCOUNTMARK" not in text


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.now += secs


def _fake_time(monkeypatch) -> _Clock:
    """A fake clock and sleep for the module's own ``invoke_sync`` binding only."""
    clock = _Clock()
    real = impl_sagemaker.invoke_sync
    monkeypatch.setattr(
        impl_sagemaker,
        "invoke_sync",
        functools.partial(real, clock=clock, sleep=clock.sleep),
    )
    return clock


def _throttle(stubber, code="ThrottlingException"):
    stubber.add_client_error(
        "invoke_endpoint", service_error_code=code, service_message="Rate exceeded"
    )


def test_throttling_is_retried_and_then_answers(stubbed, monkeypatch):
    stubber, made = stubbed
    clock = _fake_time(monkeypatch)
    _throttle(stubber)
    _throttle(stubber, "ModelNotReadyException")
    stubber.add_response("invoke_endpoint", {"Body": _body(ANSWER)})
    assert _ask()["q"].value == 0.25
    assert clock.now == pytest.approx(0.1 + 0.2)
    assert [t for _target, t in made][0] == 4.0


def test_throttling_retries_stop_at_the_attempt_cap(stubbed, monkeypatch):
    stubber, _ = stubbed
    _fake_time(monkeypatch)
    for _ in range(MAX_ATTEMPTS):
        _throttle(stubber)
    with pytest.raises(SageMakerThrottled):
        _ask()
    assert MAX_ATTEMPTS == 3


def test_a_retry_never_reads_past_the_budget(monkeypatch):
    clock = _Clock()
    timeouts: list[float] = []
    responses = iter([SageMakerThrottled("t"), SageMakerThrottled("t"), {"Body": _body(ANSWER)}])

    def fake_once(_target, _payload, read_timeout, _images=False):
        timeouts.append((clock.now, read_timeout))
        clock.now += 0.3
        item = next(responses)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(impl_sagemaker, "_invoke_once", fake_once)
    parsed, _ = invoke_sync(TARGET, {"state": "s"}, 1000, clock=clock, sleep=clock.sleep)
    assert parsed == ANSWER
    assert len(timeouts) == 3
    assert timeouts[0] == (0.0, 1.0)
    # Every attempt ends by the 1.0 s deadline: started at t, it reads at most 1.0 - t.
    for started, read_timeout in timeouts:
        assert started + read_timeout <= 1.0 + 1e-9


def test_no_retry_when_the_budget_has_no_room(monkeypatch):
    clock = _Clock()
    calls: list[float] = []

    def fake_once(_target, _payload, read_timeout, _images=False):
        calls.append(read_timeout)
        clock.now += 0.95
        raise SageMakerThrottled("t")

    monkeypatch.setattr(impl_sagemaker, "_invoke_once", fake_once)
    with pytest.raises(SageMakerThrottled):
        invoke_sync(TARGET, {"state": "s"}, 1000, clock=clock, sleep=clock.sleep)
    assert calls == [1.0]


def test_no_retry_starts_after_a_backoff_overran_the_budget(monkeypatch):
    clock = _Clock()
    calls: list[float] = []

    def fake_once(_target, _payload, read_timeout, _images=False):
        calls.append(read_timeout)
        clock.now += 0.8
        raise SageMakerThrottled("t")

    def late_sleep(secs: float) -> None:
        clock.now += secs + 0.2  # the thread wakes after the deadline

    monkeypatch.setattr(impl_sagemaker, "_invoke_once", fake_once)
    with pytest.raises(SageMakerThrottled):
        invoke_sync(TARGET, {"state": "s"}, 1000, clock=clock, sleep=late_sleep)
    assert calls == [1.0]


def test_a_timeout_is_never_retried(monkeypatch):
    calls: list[float] = []

    def fake_once(_target, _payload, read_timeout, _images=False):
        calls.append(read_timeout)
        raise SageMakerTimeout("slow")

    monkeypatch.setattr(impl_sagemaker, "_invoke_once", fake_once)
    with pytest.raises(SageMakerTimeout):
        invoke_sync(TARGET, {"state": "s"}, 5000)
    assert calls == [5.0]


def test_client_has_no_botocore_retries_and_the_budget_as_read_timeout(_no_aws):
    client = _no_aws(TARGET, 2.0)
    assert client.meta.config.retries == {"total_max_attempts": 1, "mode": "standard"}
    assert client.meta.config.read_timeout == 2.0
    assert client.meta.config.connect_timeout == 2.0


def test_missing_credentials_is_a_clear_error_and_never_sends():
    with pytest.raises(SageMakerCredentialsError):
        _ask()


def test_unknown_profile_is_a_credentials_error():
    with pytest.raises(SageMakerCredentialsError):
        _ask(f"{ENDPOINT}&profile=nobody-here")


class _RaisingClient:
    def __init__(self, exc):
        self._exc = exc

    def invoke_endpoint(self, **_kwargs):
        raise self._exc


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ReadTimeoutError(endpoint_url="https://runtime.sagemaker"), SageMakerTimeout),
        (NoCredentialsError(), SageMakerCredentialsError),
    ],
)
def test_transport_errors_map_to_clear_errors(monkeypatch, exc, expected):
    monkeypatch.setattr(impl_sagemaker, "_make_client", lambda *_a: _RaisingClient(exc))
    with pytest.raises(expected):
        _ask()


def test_missing_boto3_is_a_clear_error(monkeypatch, _no_aws):
    import builtins

    real_import = builtins.__import__

    def no_boto3(name, *args, **kwargs):
        if name == "boto3" or name.startswith("boto3."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_boto3)
    with pytest.raises(SageMakerUnavailable, match="boto3 is not installed"):
        _no_aws(TARGET, 1.0)


def test_oversized_or_non_json_answers_are_refused(stubbed):
    stubber, _ = stubbed
    raw = b"x" * (impl_sagemaker.MAX_RESPONSE_BYTES + 1)
    stubber.add_response("invoke_endpoint", {"Body": StreamingBody(io.BytesIO(raw), len(raw))})
    stubber.add_response("invoke_endpoint", {"Body": StreamingBody(io.BytesIO(b"<html>"), 6)})
    with pytest.raises(SageMakerProtocolError, match="exceeded"):
        _ask()
    with pytest.raises(SageMakerProtocolError, match="not JSON"):
        _ask()


def test_oracle_refuses_a_bad_endpoint_before_any_call(monkeypatch):
    def boom(*_a):
        raise AssertionError("no client should be built")

    monkeypatch.setattr(impl_sagemaker, "_make_client", boom)
    with pytest.raises(SageMakerConfigError):
        _ask("sagemaker://argus-9b")


# --- the host governance sees is the host the request reaches ----------------


@pytest.mark.parametrize(
    ("endpoint", "host"),
    [
        (ENDPOINT, "https://runtime.sagemaker.us-west-2.amazonaws.com"),
        (
            "sagemaker://a?region=us-gov-west-1",
            "https://runtime.sagemaker.us-gov-west-1.amazonaws.com",
        ),
        (
            "sagemaker://a?region=cn-north-1",
            "https://runtime.sagemaker.cn-north-1.amazonaws.com.cn",
        ),
    ],
)
def test_runtime_url_names_the_aws_host(endpoint, host):
    assert impl_sagemaker.runtime_url(parse_endpoint(endpoint)) == host
    assert impl_sagemaker.policy_endpoint(endpoint).startswith(host + "/endpoints/")


def test_client_is_pinned_to_that_host_whatever_the_environment_says(monkeypatch, _no_aws):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://elsewhere.example")
    monkeypatch.setenv("AWS_ENDPOINT_URL_SAGEMAKER_RUNTIME", "https://elsewhere.example")
    client = _no_aws(TARGET, 1.0)
    assert client.meta.endpoint_url == "https://runtime.sagemaker.us-west-2.amazonaws.com"


def _capture_targets(monkeypatch) -> list:
    seen: list = []

    def fake_targets_denied(_surface, *, point, model, endpoint):
        seen.append(endpoint)
        return False

    monkeypatch.setattr(capability, "targets_denied", fake_targets_denied)
    return seen


def _config(endpoint: str):
    return SimpleNamespace(decisions=SimpleNamespace(provider=_provider(endpoint)))


def test_governance_is_asked_about_the_aws_host(monkeypatch):
    seen = _capture_targets(monkeypatch)
    denied = gate._targets_denied(
        _config(ENDPOINT), None, "skills.select", "argus-9b", gate.LANE_JEV
    )
    assert not denied
    assert seen == [
        "https://runtime.sagemaker.us-west-2.amazonaws.com/endpoints/argus-9b/invocations"
    ]


def test_governance_denies_a_sagemaker_address_that_names_no_host(monkeypatch):
    seen = _capture_targets(monkeypatch)
    assert gate._targets_denied(
        _config("sagemaker://argus-9b"), None, "skills.select", "argus-9b", gate.LANE_JEV
    )
    assert seen == []


def test_governance_sees_an_http_endpoint_unchanged(monkeypatch):
    seen = _capture_targets(monkeypatch)
    url = "https://api.typesafe.ai/v1/systemone"
    gate._targets_denied(_config(url), None, "skills.select", "jev-latest", gate.LANE_JEV)
    assert seen == [url]
