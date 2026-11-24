"""The contract cases against the spec, with no gateway: what is covered, and that each
case asks for something the spec documents.

An operation the spec marks ``x-crew-status: available`` has contract cases
(``test/e2e/crew_api_contract_cases/<operationId>.py``), which
``e2e/test_crew_api_contract.py`` runs on a real gateway. While the spec carries no
such mark the check holds nothing, and it skips with that reason instead of passing.
"""

from __future__ import annotations

import json

import pytest
from e2e import crew_api_contract_spec as spec
from e2e.crew_api_contract_differences import DIFFERENCES
from e2e.crew_api_contract_spec import ANONYMOUS, OWNER, Case


def test_every_available_operation_has_contract_cases() -> None:
    available = spec.available(spec.operations())
    if not available:
        # Said out loud, not passed: with no mark in the spec this check holds nothing.
        pytest.skip("the spec marks no operation x-crew-status: available, so nothing is held")
    assert sorted(available - set(spec.cases())) == [], (
        "the spec marks these operations available and no contract case calls them: add "
        "test/e2e/crew_api_contract_cases/<operationId>.py"
    )


def test_an_available_operation_is_told_from_a_planned_one() -> None:
    def operation(mark: str) -> spec.Operation:
        return spec.Operation("op", "GET", "/v1/widgets", "crew-public.yaml", mark, {})

    found = {
        "get_v1_widgets": operation(spec.AVAILABLE),
        "post_v1_widgets": operation("planned"),
        "delete_v1_widgets": operation(""),
    }
    assert spec.available(found) == {"get_v1_widgets"}
    # The name the check above would report: no file in the cases directory has it.
    assert spec.available(found) - set(spec.cases()) == {"get_v1_widgets"}


def test_every_case_asks_for_what_the_spec_documents() -> None:
    problems = {
        f"{operation_id}:{case.name}": found
        for operation_id, cases in spec.cases().items()
        for case in cases
        if (found := spec.case_problems(operation_id, case))
    }
    assert problems == {}


def test_every_operation_file_has_a_case_that_succeeds_or_says_why_not() -> None:
    # The common refusals alone prove nothing about the operation itself.
    only_common = sorted(
        operation_id
        for operation_id, cases in spec.cases().items()
        if not any(case.caller not in (ANONYMOUS, "invalid") for case in cases)
    )
    assert only_common == []


def test_every_named_difference_is_a_case() -> None:
    ids = {f"{op}:{case.name}" for op, cases in spec.cases().items() for case in cases}
    assert sorted(set(DIFFERENCES) - ids) == []


def test_the_error_table_is_read_from_the_rfc() -> None:
    rows = spec.error_rows()
    assert (403, "permission_error", "owner_only") in rows
    assert (422, "capability_error", "capability_unavailable") in rows
    assert (422, "invalid_request_error", "idempotency_key_reused") in rows
    assert len(rows) > 20


def _reply(status: int, body: object, **headers: str) -> spec.Reply:
    sent = {"content-type": "application/json", "x-request-id": "req_1", "crew-api-version": "1.0"}
    return spec.Reply(status, {**sent, **headers}, json.dumps(body).encode())


def _error(code: str, error_type: str, request_id: str = "req_1") -> dict[str, object]:
    return {
        "error": {
            "type": error_type,
            "code": code,
            "message": "m",
            "param": None,
            "request_id": request_id,
        }
    }


def test_an_answer_that_differs_from_the_spec_is_reported() -> None:
    models = spec.operations()["get_v1_models"]
    listed = Case("lists", OWNER, 200)
    refused = Case("refused", ANONYMOUS, 401, "invalid_token")
    challenge = {"www-authenticate": 'Bearer error="invalid_token"'}
    good = _reply(200, {"object": "list", "data": []})
    unauthorized = _reply(401, _error("invalid_token", "authentication_error"), **challenge)

    def problems(case: Case, reply: spec.Reply) -> str:
        return "\n".join(spec.reply_problems(models, case, reply, "req_1"))

    assert problems(listed, good) == ""
    assert problems(refused, unauthorized) == ""
    assert "the case expects 200" in problems(listed, unauthorized)
    assert "documents no 418" in problems(listed, _reply(418, {}))
    assert "body: data" in problems(listed, _reply(200, {"object": "list", "data": {}}))
    assert "no Crew-API-Version header" in problems(
        listed,
        spec.Reply(200, {k: v for k, v in good.headers.items() if "version" not in k}, good.body),
    )
    assert "not the id the request sent" in problems(
        listed, _reply(200, {"object": "list", "data": []}, **{"x-request-id": "req_2"})
    )
    assert "no WWW-Authenticate header" in problems(
        refused, _reply(401, _error("invalid_token", "authentication_error"))
    )
    assert "has no row 401 permission_error invalid_token" in problems(
        refused, _reply(401, _error("invalid_token", "permission_error"), **challenge)
    )
    assert "the case expects 'invalid_token'" in problems(
        refused, _reply(401, _error("token_revoked", "authentication_error"), **challenge)
    )
    # An Owner operation's schema lives in crew-owner.yaml and reaches into components.yaml.
    grants = spec.operations()["get_v1_crew_owner_grants"]
    assert "body: " in "\n".join(
        spec.reply_problems(grants, listed, _reply(200, {"data": "not a list"}), "req_1")
    )
    assert "error.request_id is not" in problems(
        refused, _reply(401, _error("invalid_token", "authentication_error", "req_9"), **challenge)
    )
