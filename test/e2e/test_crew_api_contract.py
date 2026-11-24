"""The Crew API's contract, checked on a real gateway.

One gateway for the module (a real ``kirocrew gateway`` child on a scratch home, bound
to loopback, with the packaged fake ACP backend as its engine), then one test per case
in ``e2e/crew_api_contract_cases/``. ``e2e/crew_api_contract_spec.py`` says how to add an
operation.

Gated like the other real-gateway modules (``docs/ci/e2e-gate.md``): ``KIROCREW_E2E``
lifts the skip, and the cases run with ``-n0``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

import pytest
from e2e import crew_api_contract_spec as spec
from e2e.crew_api_contract_differences import DIFFERENCES
from e2e.crew_api_contract_gateway import Gateway
from e2e.crew_api_contract_spec import Case
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
def gateway() -> Iterator[Gateway]:
    with _booted("minimal") as (handle, _client):
        yield Gateway(handle)


@pytest.mark.parametrize(("operation_id", "case"), _params())
def test_the_gateway_answers_as_the_spec_says(
    gateway: Gateway, operation_id: str, case: Case
) -> None:
    operation = spec.operations()[operation_id]
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
