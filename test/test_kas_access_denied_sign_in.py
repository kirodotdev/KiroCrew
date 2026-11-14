"""KAS's refused-credential answer is a sign-in error that says where to sign in.

KAS words an ``AccessDeniedError`` as "Access denied. Please check your
authentication. (Request ID: ...)", with no 401/403 and none of the expiry
wording the prose vocabulary read. The turn then showed that sentence alone,
which names no place to sign in. It now takes the session-expiry arm: the
harness's signed-out message (the Kiro sign-in card on KAS, ``kiro-cli login``
on the Kiro CLI harness), the ``sign_in_needed`` tag, and ``auth_required``
where the card is the fix. The request id stays, after the message.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.client import AcpError, _format_acp_error, _raise_acp_error
from kiro_crew.acp.transport_errors import is_auth_failure_output

_REQUEST_ID = "69c7830a-1b2c-4d5e-8f90-0123456789ab"
_KAS_TEXT = f"Access denied. Please check your authentication. (Request ID: {_REQUEST_ID})"
_KAS_SIGNED_OUT = (
    "Not signed in to Kiro. Sign in from Settings → Agent Harness → Kiro Agent → "
    "Kiro sign-in, then send your message again."
)
_KIRO_SIGNED_OUT = "kiro-cli is not logged in. Run `kiro-cli login` in your terminal"


def _frames() -> list[dict]:
    return [
        {
            "code": -32603,
            "message": "Internal error",
            "data": f"{_KAS_TEXT} request_id: {_REQUEST_ID}",
        },
        # The bundled engine's frame as measured on a gateway with a refused sign-in.
        {
            "code": -32000,
            "message": _KAS_TEXT,
            "data": {
                "errorType": "AccessDeniedError",
                "retryErrorType": "CLIENT_ERROR",
                "requestId": _REQUEST_ID,
            },
        },
    ]


@pytest.mark.parametrize("frame", _frames(), ids=["prose-data", "structured-data"])
def test_kas_tags_it_auth_required_with_the_sign_in_place(frame):
    with pytest.raises(AcpError) as raised:
        _raise_acp_error(frame, None, backend="kas")
    err = raised.value
    assert err.sign_in_needed is True
    assert err.auth_required is True
    assert err.transient is False
    assert str(err).startswith(_KAS_SIGNED_OUT)
    assert "Access denied" not in str(err)


def test_the_request_id_follows_the_message_once():
    out = _format_acp_error(_frames()[0], backend="kas")
    assert out.endswith(f"(request_id: {_REQUEST_ID})")
    assert out.count(_REQUEST_ID) == 1


def test_the_kiro_cli_harness_names_its_own_sign_in():
    """Harness parity H6: no Kiro card for a harness on kiro-cli's own login."""
    with pytest.raises(AcpError) as raised:
        _raise_acp_error(_frames()[0], None, backend="")
    assert raised.value.sign_in_needed is True
    assert raised.value.auth_required is False
    assert str(raised.value).startswith(_KIRO_SIGNED_OUT)


def test_the_same_words_on_stderr_are_an_auth_failure():
    assert is_auth_failure_output(_KAS_TEXT) is True


@pytest.mark.parametrize(
    "text",
    ["Access denied to /tmp/x", "AccessDeniedError while reading the cache"],
)
def test_other_access_denied_text_is_not_a_sign_in_error(text):
    frame = {"code": -32603, "message": "Internal error", "data": text}
    with pytest.raises(AcpError) as raised:
        _raise_acp_error(frame, None, backend="kas")
    assert raised.value.sign_in_needed is False
