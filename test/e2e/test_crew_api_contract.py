"""The Crew API's contract, checked on a real gateway.

One gateway for the module (a real ``kirocrew gateway`` child on a scratch home, bound
to loopback, with the packaged fake ACP backend as its engine), then one test per case
in ``e2e/crew_api_contract_cases/``. ``e2e/crew_api_contract_spec.py`` says how to add an
operation.

The engine is the packaged backend behind ``e2e/crew_api_contract_gated_engine.py``, so
a turn can raise a tool approval that waits for the owner. It runs no command, and the
module ends by checking that the command it names was never run.

The four local authorization routes are in no spec file ("No OpenAPI; parameters follow
OAuth 2.1 and RFC 8414", ``rfc-crew-api.md``), so they have no cases. The tests at the
end of this module hold them to the sentences the RFC does write, and each says which.

Gated like the other real-gateway modules (``docs/ci/e2e-gate.md``): ``KIROCREW_E2E``
lifts the skip, and the cases run with ``-n0``.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from e2e import crew_api_contract_gated_engine as gated_engine
from e2e import crew_api_contract_spec as spec
from e2e.crew_api_contract_differences import DIFFERENCES
from e2e.crew_api_contract_gateway import REDIRECT, Asking, Gateway, redirect_query
from e2e.crew_api_contract_spec import Case, Reply
from e2e.test_gateway_boot_matrix import _booted

pytestmark = pytest.mark.skipif(
    not os.environ.get("KIROCREW_E2E"),
    reason="Boots a real gateway. Set KIROCREW_E2E=1 to run.",
)


def _params() -> list[object]:
    params = []
    for operation_id, cases in sorted(spec.cases().items()):
        for case in cases:
            case_id = f"{operation_id}:{case.name}"
            marks = (
                [pytest.mark.xfail(strict=True, reason=DIFFERENCES[case_id])]
                if case_id in DIFFERENCES
                else []
            )
            params.append(pytest.param(operation_id, case, id=case_id, marks=marks))
    return params


@pytest.fixture(scope="module")
def gateway(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Gateway]:
    # The command the wrapped backend asks approval for. Nothing runs it.
    target = tmp_path_factory.mktemp("gated-command") / "never-created"

    def launcher(directory: Path) -> Path:
        return gated_engine.launcher(directory, f"touch {target}")

    with _booted("minimal", launcher) as (handle, _client):
        served = Gateway(handle)
        try:
            yield served
        finally:
            # No turn may wait on an approval when the gateway stops.
            served.settle()
    assert not target.exists(), "the backend ran the command it asked approval for"


@pytest.mark.parametrize(("operation_id", "case"), _params())
def test_the_gateway_answers_as_the_spec_says(
    gateway: Gateway, operation_id: str, case: Case
) -> None:
    operation = spec.operations()[operation_id]
    try:
        reply, request_id = gateway.send(operation, case)
        problems = spec.reply_problems(operation, case, reply, request_id)
        try:
            body = json.loads(reply.body)
        except ValueError:
            body = None
        assert not problems, (
            f"{operation.method} {operation.path} as {case.caller}:\n  "
            + "\n  ".join(problems)
            + f"\n{gateway.diagnostics()}"
        )
        if case.check is not None:
            case.check(body)
    finally:
        # A case that raised an approval leaves no turn waiting on it.
        gateway.settle()


def _json(reply: Reply) -> Any:
    return json.loads(reply.body)


def _jwt_part(token: str, index: int) -> dict[str, Any]:
    part = token.split(".")[index]
    return dict(json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))))


def _approved(gateway: Gateway) -> tuple[Asking, str]:
    """An app the owner approved, and the one-time code its next ask was redirected with."""
    asking = gateway.ask()
    assert asking.first.status == 202, asking.first.status
    gateway.decide(asking, "approve")
    again = gateway.ask_again(asking)
    assert again.status == 302, again.status
    return asking, redirect_query(again)["code"]


def test_the_metadata_names_this_gateways_local_authorization_routes(gateway: Gateway) -> None:
    # RFC: the four routes are on the local gateway, and "parameters follow OAuth 2.1
    # and RFC 8414". RFC 8414 section 2 requires the first four fields read here.
    reply = gateway.metadata()
    assert reply.status == 200, reply.status
    body = _json(reply)
    assert body["issuer"] == gateway.origin
    assert body["authorization_endpoint"] == gateway.origin + "/oauth/authorize"
    assert body["token_endpoint"] == gateway.origin + "/oauth/token"
    assert body["response_types_supported"] == ["code"]
    # RFC: local authorization "runs authorization code + PKCE".
    assert "S256" in body["code_challenge_methods_supported"]
    # RFC 8414 names this field for a server that has a revocation route.
    assert body["revocation_endpoint"] == gateway.origin + "/oauth/revoke"


def test_a_first_ask_records_a_request_and_grants_nothing(gateway: Gateway) -> None:
    # RFC, step 1: "202 with request_id, user_code, expires_at and interval for a
    # program that accepts application/json". And: "GET /oauth/authorize never grants
    # by itself".
    first = gateway.ask().first
    assert first.status == 202, first.status
    body = _json(first)
    assert all(body[name] for name in ("request_id", "user_code", "expires_at", "interval")), body
    assert "location" not in first.headers


def test_an_approved_app_is_redirected_with_a_code_that_redeems_once(gateway: Gateway) -> None:
    asking = gateway.ask()
    gateway.decide(asking, "approve")
    # RFC, step 3: "Once the owner answered, the gateway redirects to the app's
    # loopback redirect_uri with a one-time code".
    again = gateway.ask_again(asking)
    assert again.status == 302, again.status
    assert again.headers["location"].startswith(REDIRECT + "?")
    answer = redirect_query(again)
    assert answer.get("code") and "error" not in answer, sorted(answer)

    redeemed = gateway.exchange(asking, answer["code"])
    assert redeemed.status == 200, redeemed.status
    token = _json(redeemed)["access_token"]
    # RFC: "The access token is a JWT with typ kiro-crew-local+jwt, valid 5 minutes."
    assert _jwt_part(token, 0)["typ"] == "kiro-crew-local+jwt"
    claims = _jwt_part(token, 1)
    assert claims["exp"] - claims["iat"] == 300
    # The token is the app's credential: it reads its own grant with it.
    assert gateway.read_own_grant(token).status == 200

    # "one-time": the same code redeems nothing the second time.
    assert gateway.exchange(asking, answer["code"]).status == 400


def test_a_denied_app_is_redirected_with_access_denied(gateway: Gateway) -> None:
    asking = gateway.ask()
    gateway.decide(asking, "deny")
    # RFC, step 3: "or with error=access_denied".
    again = gateway.ask_again(asking)
    assert again.status == 302, again.status
    assert again.headers["location"].startswith(REDIRECT + "?")
    answer = redirect_query(again)
    assert answer.get("error") == "access_denied" and "code" not in answer, sorted(answer)


def test_a_code_redeems_nothing_without_the_apps_verifier(gateway: Gateway) -> None:
    # RFC: local authorization "runs authorization code + PKCE", so the code alone,
    # with a verifier the app did not make, is refused.
    asking, code = _approved(gateway)
    assert gateway.exchange(asking, code, verifier="x" * 64).status == 400


def test_an_app_gives_its_grant_back(gateway: Gateway) -> None:
    # The RFC lists POST /oauth/revoke and writes no answer for it. Held here: the
    # route answers 200, and after that the token it was given is refused.
    asking, code = _approved(gateway)
    token = _json(gateway.exchange(asking, code))["access_token"]
    assert gateway.read_own_grant(token).status == 200
    assert gateway.revoke(token).status == 200
    assert gateway.read_own_grant(token).status == 401
