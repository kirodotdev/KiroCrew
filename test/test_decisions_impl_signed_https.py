"""``impl_signed_https``: decisions sent to an HTTPS endpoint signed with AWS SigV4.

Pinned here:

* the ``aws-https://`` address is parsed strictly, and :func:`build_endpoint` accepts
  any ``https`` host name, port and path while refusing everything else (another
  scheme, user info, an IP literal, a query, a fragment, a traversal, a bad service,
  region or profile);
* the provider registry routes such an address to this client, gives it its own
  allow-list id, and leaves ``sagemaker://`` and plain ``https://`` addresses where
  they were;
* the request goes to the ``https://`` URL the address names, carries the System One
  body, is signed for the configured service and region, and never sends a bearer key;
* a non-2xx, a refused signature, a redirect and a body that is not a System One
  answer are each an error, never a partial answer.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from kiro_crew.decisions import capability, impl_sagemaker, impl_signed_https
from kiro_crew.decisions import provider as provider_mod
from kiro_crew.decisions.impl_signed_https import (
    SignedHttpsAddressFieldError,
    SignedHttpsConfigError,
    SignedHttpsCredentialsError,
    SignedHttpsHttpError,
    SignedHttpsProvider,
    build_endpoint,
    parse_endpoint,
    policy_endpoint,
)
from kiro_crew.decisions.impl_systemone import ProviderProtocolError
from kiro_crew.decisions.types import Choice, Noul

URL = "https://decider.example.com/v1/decisions"
ADDRESS = "aws-https://decider.example.com/v1/decisions?service=execute-api&region=us-west-2"


class TestBuildEndpoint:
    @pytest.mark.parametrize(
        ("url", "service", "region", "profile", "expected"),
        [
            (URL, "execute-api", "us-west-2", "", ADDRESS),
            (
                "https://Decider.Example.com/v1/decisions",
                "execute-api",
                "us-west-2",
                "",
                ADDRESS,
            ),
            (
                "https://api.dp.decider.example.com:8443/v2/run",
                "lambda",
                "eu-central-1",
                "research",
                "aws-https://api.dp.decider.example.com:8443/v2/run"
                "?service=lambda&region=eu-central-1&profile=research",
            ),
            (
                "https://decider.example.com",
                "execute-api",
                "us-gov-west-1",
                "",
                "aws-https://decider.example.com/?service=execute-api&region=us-gov-west-1",
            ),
        ],
    )
    def test_any_https_host_and_path_is_accepted(self, url, service, region, profile, expected):
        assert build_endpoint(url, service, region, profile) == expected
        target = parse_endpoint(expected)
        assert target.service == service and target.region == region
        assert target.profile == profile

    @pytest.mark.parametrize(
        "url",
        [
            "http://decider.example.com/v1/decisions",
            "ftp://decider.example.com/v1/decisions",
            "decider.example.com/v1/decisions",
            "https://user:pw@decider.example.com/v1/decisions",
            "https://decider.example.com/v1/decisions?x=1",
            "https://decider.example.com/v1/decisions#frag",
            "https://decider.example.com/v1/../admin",
            "https://decider.example.com//v1",
            "https://decider.example.com/v1/dec isions",
            "https://decider.example.com/v1/%2e%2e/admin",
            "https://127.0.0.1/v1/decisions",
            "https://[::1]/v1/decisions",
            "https://decider.example.com:0/v1",
            "https://decider.example.com:99999/v1",
            "https://decider.example.com:08443/v1",
            "https://-bad.example.com/v1",
            "https:///v1/decisions",
            "",
        ],
    )
    def test_a_url_that_is_not_a_plain_https_host_and_path_is_refused(self, url):
        with pytest.raises(SignedHttpsAddressFieldError) as caught:
            build_endpoint(url, "execute-api", "us-west-2")
        assert caught.value.field == "url"

    def test_a_non_string_url_is_refused(self):
        with pytest.raises(SignedHttpsAddressFieldError):
            build_endpoint(None, "execute-api", "us-west-2")

    @pytest.mark.parametrize("service", ["", "Execute-API", "exec api", "-x", "a" * 64, None])
    def test_a_bad_service_is_refused(self, service):
        with pytest.raises(SignedHttpsAddressFieldError) as caught:
            build_endpoint(URL, service, "us-west-2")
        assert caught.value.field == "service"

    @pytest.mark.parametrize("region", ["", "uswest2", "US-WEST-2", "us-west-2&profile=x", None])
    def test_a_bad_region_is_refused(self, region):
        with pytest.raises(SignedHttpsAddressFieldError) as caught:
            build_endpoint(URL, "execute-api", region)
        assert caught.value.field == "region"

    @pytest.mark.parametrize("profile", ["has space", "a&region=x", "-lead"])
    def test_a_bad_profile_is_refused(self, profile):
        with pytest.raises(SignedHttpsAddressFieldError) as caught:
            build_endpoint(URL, "execute-api", "us-west-2", profile)
        assert caught.value.field == "profile"


class TestParseEndpoint:
    @pytest.mark.parametrize(
        "address",
        [
            URL,
            "sagemaker://decision-model-a?region=us-west-2",
            "aws-https://decider.example.com/v1?region=us-west-2",
            "aws-https://decider.example.com/v1?service=execute-api",
            "aws-https://decider.example.com/v1?service=execute-api&region=us-west-2&x=1",
            "aws-https://decider.example.com/v1?service=a&service=b&region=us-west-2",
            "aws-https://u@decider.example.com/v1?service=execute-api&region=us-west-2",
            "aws-https://decider.example.com/v1?service=execute-api&region=us-west-2#f",
            "aws-https://10.0.0.1/v1?service=execute-api&region=us-west-2",
        ],
    )
    def test_anything_but_the_exact_shape_is_refused(self, address):
        with pytest.raises(SignedHttpsConfigError):
            parse_endpoint(address)

    def test_the_policy_url_is_the_https_url_the_request_reaches(self):
        assert policy_endpoint(ADDRESS) == URL


class TestProviderSelection:
    def test_an_aws_https_address_selects_this_client(self):
        spec = provider_mod.select(provider_mod.LANE_ENDPOINT, endpoint=ADDRESS, model="m")
        assert spec.id == provider_mod.PROVIDER_SIGNED_HTTPS
        # Text only: whether such an endpoint reads images is not known.
        assert spec.supports == provider_mod.QUESTION_KINDS
        assert isinstance(spec.build(_cfg(), "m"), SignedHttpsProvider)

    def test_sagemaker_and_plain_https_addresses_are_unchanged(self):
        sm = provider_mod.select(
            provider_mod.LANE_ENDPOINT,
            endpoint="sagemaker://decision-model-a?region=us-west-2",
            model="m",
        )
        assert sm.id == provider_mod.PROVIDER_SAGEMAKER
        hosted = provider_mod.select(provider_mod.LANE_ENDPOINT, endpoint=URL, model="m")
        assert hosted.id == provider_mod.PROVIDER_HOSTED

    def test_the_address_has_its_own_allow_list_id(self):
        assert capability.provider_id(ADDRESS, "m") == capability.PROVIDER_SIGNED_HTTPS
        assert capability.PROVIDER_SIGNED_HTTPS in capability.provider_ids()
        # A malformed one is still this provider, as a malformed sagemaker:// is.
        assert capability.provider_id("aws-https://", "m") == capability.PROVIDER_SIGNED_HTTPS
        assert capability.provider_id(URL, "m") is None

    def test_the_sagemaker_scheme_test_does_not_claim_it(self):
        assert not impl_sagemaker.is_sagemaker_endpoint(ADDRESS)

    def test_an_lts_build_still_refuses_the_jev_host_behind_this_scheme(self, monkeypatch):
        """The send path asks the host lock about the https URL, so the scheme is no way round it."""
        monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")
        jev = "aws-https://api.typesafe.ai/v1/systemone?service=execute-api&region=us-west-2"
        assert capability.targets_denied(
            point=None, model=None, endpoint=policy_endpoint(jev), audit=False
        )


# --- the request on the wire --------------------------------------------------


class _Creds:
    access_key = "AKIDEXAMPLE"
    secret_key = "secret-example"
    token = None


def _cfg(endpoint: str = ADDRESS, model: str = "decider-model-v2", timeout_ms: int = 5000):
    from types import SimpleNamespace

    return SimpleNamespace(endpoint=endpoint, model=model, timeout_ms=timeout_ms)


URGENT = Choice(id="is_urgent", prompt="Does this convey urgency?", options=["yes", "no"])
OK_BODY = {
    "model": "decider-model-v2",
    "answers": {
        "is_urgent": {
            "type": "choice",
            "choice": "yes",
            "probabilities": {"yes": 0.9, "no": 0.1},
            "confidence": 0.8,
        }
    },
    "usage": {"input_tokens": 12, "output_tokens": 0},
}


@pytest.fixture
def wire(monkeypatch):
    """Route the shared client through a recording transport; returns (seen, set_reply)."""
    from kiro_crew.decisions import impl_systemone

    seen: list[httpx.Request] = []
    reply: dict = {"status": 200, "body": OK_BODY, "headers": {"x-amzn-RequestId": "req-1"}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = reply["body"]
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        return httpx.Response(reply["status"], content=content, headers=reply["headers"])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    monkeypatch.setattr(impl_systemone, "_session_for_running_loop", lambda: client)
    monkeypatch.setattr(impl_signed_https, "_frozen_credentials", lambda _profile: _Creds())
    return seen, reply.update


def _ask(questions=(URGENT,), cfg=None):
    client = SignedHttpsProvider(cfg or _cfg())
    answers = asyncio.run(client.ask("The user says: help, now!", list(questions)))
    return client, answers


class TestRequest:
    def test_the_signed_request_goes_to_the_named_url_with_the_system_one_body(self, wire):
        seen, _ = wire
        client, answers = _ask()
        assert answers["is_urgent"].value == "yes"
        (request,) = seen
        assert str(request.url) == URL
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body["model"] == "decider-model-v2"
        assert body["state"] == "The user says: help, now!"
        assert body["questions"]["is_urgent"]["type"] == "choice"
        auth = request.headers["authorization"]
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
        assert "/us-west-2/execute-api/aws4_request" in auth
        assert "x-amz-date" in request.headers
        assert "Bearer" not in auth
        assert client.last_response_meta == {
            "provider_model": "decider-model-v2",
            "input_tokens": 12,
            "output_tokens": 0,
            "request_id": "req-1",
        }

    def test_the_signature_covers_the_exact_body_sent(self, wire):
        """Re-signing the bytes that went over the wire yields the same signature."""
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials

        seen, _ = wire
        _ask()
        (request,) = seen
        again = AWSRequest(
            method="POST",
            url=URL,
            data=request.content,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        again.context["timestamp"] = request.headers["x-amz-date"]
        SigV4Auth(
            Credentials("AKIDEXAMPLE", "secret-example"), "execute-api", "us-west-2"
        ).add_auth(again)
        assert again.headers["Authorization"] == request.headers["authorization"]

    def test_a_noul_question_round_trips(self, wire):
        _, set_reply = wire
        set_reply(body={"answers": {"q": {"type": "noul", "noul": 0.2}}})
        _, answers = _ask((Noul(id="q", prompt="Is it?"),))
        assert answers["q"].value == 0.2

    @pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
    def test_a_non_2xx_is_an_error(self, wire, status):
        _, set_reply = wire
        set_reply(status=status, body={"message": "nope"})
        with pytest.raises(SignedHttpsHttpError) as caught:
            _ask()
        assert caught.value.status == status
        assert "nope" not in str(caught.value)

    @pytest.mark.parametrize("status", [401, 403])
    def test_a_refused_signature_is_a_credentials_error(self, wire, status):
        _, set_reply = wire
        set_reply(status=status, body={})
        with pytest.raises(SignedHttpsCredentialsError):
            _ask()

    @pytest.mark.parametrize("status", [301, 302, 307, 308])
    def test_a_redirect_is_refused_and_never_followed(self, wire, status):
        seen, set_reply = wire
        set_reply(status=status, body={}, headers={"Location": "https://elsewhere.example.com/"})
        with pytest.raises(SignedHttpsHttpError):
            _ask()
        assert len(seen) == 1

    def test_a_body_that_is_not_a_system_one_answer_is_an_error(self, wire):
        _, set_reply = wire
        set_reply(body={"answers": {}})
        with pytest.raises(ProviderProtocolError):
            _ask()
        set_reply(body=b"not json")
        with pytest.raises(ProviderProtocolError):
            _ask()

    def test_an_invalid_address_sends_nothing(self, wire):
        seen, _ = wire
        with pytest.raises(SignedHttpsConfigError):
            _ask(cfg=_cfg(endpoint="aws-https://decider.example.com/v1?region=us-west-2"))
        assert seen == []

    def test_missing_credentials_send_nothing(self, wire, monkeypatch):
        seen, _ = wire

        def _none(_profile):
            raise SignedHttpsCredentialsError("no AWS credentials were found")

        monkeypatch.setattr(impl_signed_https, "_frozen_credentials", _none)
        with pytest.raises(SignedHttpsCredentialsError):
            _ask()
        assert seen == []
