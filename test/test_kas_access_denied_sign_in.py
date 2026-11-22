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
    "Not signed in to Kiro. Sign in from Settings → Kiro account, then send your message again."
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


def test_the_kiro_cli_harness_row_asks_for_kiro_clis_sign_in_commands():
    """The row shows the setup gate's two kiro-cli sign-in commands, not Crew's card."""
    from kiro_crew.acp.transport_errors import AcpAuthRequired
    from kiro_crew.dashboard.chat_turn.recovery import _terminal_error_meta

    with pytest.raises(AcpError) as raised:
        _raise_acp_error(_frames()[1], None, backend="")
    assert raised.value.kiro_cli_sign_in is True
    assert _terminal_error_meta(raised.value) == {"kind": "kiro_cli_sign_in"}
    spawn = AcpAuthRequired(_KIRO_SIGNED_OUT, backend="")
    assert spawn.auth_required is False
    assert _terminal_error_meta(spawn) == {"kind": "kiro_cli_sign_in"}


def test_a_kas_row_never_asks_for_kiro_clis_commands():
    from kiro_crew.acp.transport_errors import AcpAuthRequired

    with pytest.raises(AcpError) as raised:
        _raise_acp_error(_frames()[1], None, backend="kas")
    assert raised.value.kiro_cli_sign_in is False
    assert AcpAuthRequired("x", backend="kas").kiro_cli_sign_in is False


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        (None, {"kind": "auth_required"}),
        ("bundle", {"kind": "auth_required"}),
        ("relay", {"kind": "kiro_cli_sign_in", "crew_sign_in": True}),
    ],
)
def test_a_signed_out_relay_offers_both_sign_ins(monkeypatch, route, expected):
    """kiro-cli's relay runs on Crew's sign-in when its vault holds one, else on
    kiro-cli's, so its row offers kiro-cli's commands and the Sign in button. A
    KAS pick and a stored Kiro CLI pick both reach the relay through this route.
    The bundle keeps the Sign in button alone."""
    from kiro_crew.acp import kas_route
    from kiro_crew.acp.transport_errors import AcpAuthRequired
    from kiro_crew.dashboard.chat_turn.recovery import _terminal_error_meta

    live = {
        None: None,
        "bundle": kas_route.KasRoute(bundle=True, reason=kas_route.REASON_CONFIGURED),
        "relay": kas_route.KasRoute(bundle=False),
    }[route]
    monkeypatch.setattr(kas_route, "_LAST_SPAWN_ROUTE", live)
    meta = _terminal_error_meta(AcpAuthRequired("Not signed in.", backend="kas"))
    assert {k: meta[k] for k in expected} == expected
    assert ("crew_sign_in" in meta) is (route == "relay")


@pytest.mark.parametrize(("crew_auth", "crew_sign_in"), [(False, False), (True, True)])
def test_a_launcher_relay_offers_crew_sign_in_only_when_the_vault_answers_it(
    monkeypatch, crew_auth, crew_sign_in
):
    """A relay pinned to kiro-cli's launcher runs on kiro-cli's sign-in alone unless
    Crew's vault answers it, so the Sign in button is left out otherwise."""
    from kiro_crew.acp import kas_route
    from kiro_crew.acp.transport_errors import AcpAuthRequired
    from kiro_crew.dashboard.chat_turn.recovery import _terminal_error_meta

    route = kas_route.KasRoute(
        bundle=False, reason=kas_route.REASON_KIRO_CLI_LAUNCHER, crew_auth=crew_auth
    )
    monkeypatch.setattr(kas_route, "_LAST_SPAWN_ROUTE", route)
    meta = _terminal_error_meta(AcpAuthRequired("Not signed in.", backend="kas"))
    assert meta["kind"] == "kiro_cli_sign_in"
    assert ("crew_sign_in" in meta) is crew_sign_in
