"""The engine's structured SignInRequiredError is a sign-in error.

Kiro Crew's bundled engine (KAS 0.66.104) answers a signed-out ``session/prompt``
with ``data.errorType == "SignInRequiredError"`` and none of the 401/403 or
"not signed in" wording the prose vocabulary reads. The frame is the live one in
``test/fixtures/acp_frames/kas/sign-in-required-live.jsonl``. Only the
structured field decides; the message text is never matched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.acp.client import AcpError, _format_acp_error, _raise_acp_error

_FIXTURE = Path(__file__).parent / "fixtures/acp_frames/kas/sign-in-required-live.jsonl"

_SWITCH = " Switching models will not help — this is a sign-in issue, not a backend error."
_KAS_SIGNED_OUT = (
    "Not signed in to Kiro. Sign in from Settings → Agent Harness → Kiro Agent → "
    "Kiro sign-in, then send your message again."
)
_KIRO_SIGNED_OUT = (
    "kiro-cli is not logged in. Run `kiro-cli login` in your terminal, then send your "
    "message again."
)


def _captured_error() -> dict:
    frames = [json.loads(line) for line in _FIXTURE.read_text(encoding="utf-8").splitlines()]
    errors = [f["error"] for f in frames[1:] if "error" in f]
    assert len(errors) == 1
    return errors[0]


def _frames() -> list[dict]:
    error = _captured_error()
    as_text = dict(error, data=json.dumps(error["data"]))
    return [error, as_text]


@pytest.mark.parametrize("frame", _frames(), ids=["data-dict", "data-json-text"])
def test_kas_tags_it_auth_required_with_the_signed_out_message(frame):
    with pytest.raises(AcpError) as raised:
        _raise_acp_error(frame, None, backend="kas")
    err = raised.value
    assert err.sign_in_needed is True
    assert err.auth_required is True
    assert err.transient is False
    assert err.user_worded is True
    assert str(err) == _KAS_SIGNED_OUT + _SWITCH


@pytest.mark.parametrize("frame", _frames(), ids=["data-dict", "data-json-text"])
def test_a_harness_without_the_host_sign_in_gets_no_kiro_card(frame):
    """Harness parity H6: the card would sign in the wrong identity there."""
    with pytest.raises(AcpError) as raised:
        _raise_acp_error(frame, None, backend="")
    err = raised.value
    assert err.sign_in_needed is True
    assert err.auth_required is False
    assert err.transient is False
    assert str(err) == _KIRO_SIGNED_OUT + _SWITCH


@pytest.mark.parametrize(
    "frame",
    [
        {"code": -32000, "message": "SignInRequiredError", "data": {"errorType": "Other"}},
        {"code": -32000, "message": "Sign in to Kiro to send a prompt.", "data": None},
        {"code": -32603, "message": "Internal error", "data": "SignInRequiredError"},
    ],
    ids=["name-in-message", "same-message-no-data", "name-in-prose-data"],
)
def test_only_the_structured_field_decides(frame):
    from kiro_crew.acp.sign_in_required_errors import is_sign_in_required

    assert is_sign_in_required(frame["data"]) is False
    with pytest.raises(AcpError) as raised:
        _raise_acp_error(frame, None, backend="kas")
    assert raised.value.sign_in_needed is False
    assert raised.value.auth_required is False
    assert not _format_acp_error(frame, backend="kas").startswith(_KAS_SIGNED_OUT)


def test_a_bedrock_credential_answer_keeps_its_own_remedy():
    """``_RE_AUTH`` stays ahead of the sign-in arm, as it is for session expiry."""
    frame = dict(
        _captured_error(),
        message="ExpiredTokenException: The security token included in the request is expired",
    )
    with pytest.raises(AcpError) as raised:
        _raise_acp_error(frame, None, backend="kas")
    assert raised.value.sign_in_needed is False
    assert not str(raised.value).startswith(_KAS_SIGNED_OUT)
