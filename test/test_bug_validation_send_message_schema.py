"""RED test for SEND_MESSAGE_SCHEMA caller_session pattern defect.

The caller_session FieldSpec in SEND_MESSAGE_SCHEMA must accept the same
two-segment ``cron:<job_id>:<run_id>`` form that CRON_SESSION_RE documents and
accepts. The buggy inline pattern ``^(cron:[a-zA-Z0-9]+)?$`` only matches the
single-segment ``cron:<job_id>`` form, so a legitimate two-segment caller_session
is wrongly rejected.
"""

from __future__ import annotations

import pytest

from kiro_crew.validation import (
    CRON_SESSION_RE,
    SEND_MESSAGE_SCHEMA,
    ValidationError,
    validate_field,
)


def _caller_session_spec():
    for spec in SEND_MESSAGE_SCHEMA.fields:
        if spec.name == "caller_session":
            return spec
    raise AssertionError("caller_session field not found in SEND_MESSAGE_SCHEMA")


def test_agent_defect():
    value = "cron:job1:run2"

    # CRON_SESSION_RE is the documented authority and accepts the two-segment form.
    assert CRON_SESSION_RE.match(value), "CRON_SESSION_RE should accept cron:<job_id>:<run_id>"

    spec = _caller_session_spec()
    # The schema's caller_session field must agree: validation must not reject it.
    try:
        cleaned = validate_field(value, spec)
    except ValidationError as exc:
        pytest.fail(
            f"caller_session pattern rejected documented two-segment form {value!r}: {exc}"
        )
    assert cleaned == value
