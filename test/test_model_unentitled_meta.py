"""The structural tag a model-entitlement rejection leaves on its error row.

``chat_runner._model_unentitled_meta`` decides from the exception's
``rejected_model`` / ``advertised`` attributes (set by ``_raise_acp_error``),
never from the prose, whether the terminal error row gets the
``model_unentitled`` kind the frontend turns into fix affordances.
"""

from kiro_crew.acp.client import AcpError
from kiro_crew.dashboard.chat_runner import _model_unentitled_meta
from kiro_crew.dashboard.chat_utils import MODEL_UNENTITLED_KIND


def _exc(rejected, advertised):
    e = AcpError("boom", transient=False)
    e.rejected_model = rejected
    e.advertised = advertised
    return e


def test_rejected_id_absent_from_advertised_is_tagged():
    # Bare kind only — the same shape every TRANSIENT_RETRY_KIND append persists.
    # The rejected id and the served list already live in the row's prose.
    meta = _model_unentitled_meta(_exc("auto", ["gpt-5.6-sol", "glm-5"]))
    assert meta == {"kind": MODEL_UNENTITLED_KIND}


def test_rejected_id_present_in_advertised_is_a_capacity_blip_not_entitlement():
    # Same verdict _model_is_unentitled reaches: an advertised model that was
    # rejected is transient, so no fix affordance.
    assert _model_unentitled_meta(_exc("glm-5", ["gpt-5.6-sol", "glm-5"])) is None
    assert _model_unentitled_meta(_exc("GLM-5", ["glm-5"])) is None


def test_unknowable_without_advertised_list():
    assert _model_unentitled_meta(_exc("auto", [])) is None
    assert _model_unentitled_meta(_exc("auto", None)) is None


def test_untagged_exception_yields_none():
    assert _model_unentitled_meta(AcpError("plain")) is None
    assert _model_unentitled_meta(RuntimeError("x")) is None
    assert _model_unentitled_meta(_exc("", ["a"])) is None
    assert _model_unentitled_meta(_exc("   ", ["a"])) is None


def test_verdict_is_the_shared_predicate():
    # The helper defers to client.model_is_unusable, so the two cannot drift.
    from kiro_crew.acp.client import model_is_unusable

    for rejected, adv in [("auto", ["glm-5"]), ("glm-5", ["glm-5"]), ("auto", []), ("auto", None)]:
        expected = {"kind": MODEL_UNENTITLED_KIND} if model_is_unusable(rejected, adv) else None
        assert _model_unentitled_meta(_exc(rejected, adv)) == expected


# ---- the sign-in tag ---------------------------------------------------------


def test_terminal_error_meta_reads_the_auth_required_tag():
    from kiro_crew.dashboard.chat_runner import _terminal_error_meta
    from kiro_crew.dashboard.chat_utils import AUTH_REQUIRED_KIND

    e = AcpError("Your session has expired.", transient=False)
    assert _terminal_error_meta(e) is None
    e.auth_required = True
    assert _terminal_error_meta(e) == {"kind": AUTH_REQUIRED_KIND}


def test_terminal_error_meta_prefers_the_entitlement_verdict():
    from kiro_crew.dashboard.chat_runner import _terminal_error_meta

    e = _exc("auto", ["gpt-5.6-sol"])
    e.auth_required = True
    assert _terminal_error_meta(e) == {"kind": MODEL_UNENTITLED_KIND}


def test_raise_acp_error_tags_session_expiry_from_the_raw_frame():
    import pytest

    from kiro_crew.acp.client import _raise_acp_error
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    # The engine's own wording for a Crew-owned relay whose credential callback
    # was refused, and a bare 401: both are a sign-in problem.
    for frame in (
        {"code": -32000, "message": "You are not signed in. Please sign in and retry."},
        {"code": -32603, "message": "request failed", "data": "HTTP 401 Unauthorized"},
    ):
        with pytest.raises(AcpError) as excinfo:
            _raise_acp_error(frame, backend=ACP_BACKEND_KAS)
        assert getattr(excinfo.value, "auth_required", False) is True
    # A Bedrock-named credential exception has a different remedy (AWS
    # credentials), and a plain 5xx is not a sign-in problem at all.
    for frame in (
        {"code": -32603, "message": "ExpiredTokenException: the security token is expired"},
        {"code": -32603, "message": "HTTP 503 Service Unavailable"},
    ):
        with pytest.raises(AcpError) as excinfo:
            _raise_acp_error(frame, backend=ACP_BACKEND_KAS)
        assert getattr(excinfo.value, "auth_required", False) is False


# ---- the usage-limit tag ---------------------------------------------------------


def test_terminal_error_meta_reads_the_usage_limit_tag():
    from kiro_crew.dashboard.chat_runner import _terminal_error_meta
    from kiro_crew.dashboard.chat_utils import USAGE_LIMIT_KIND

    e = AcpError("The monthly usage limit has been reached.", transient=False)
    # Prose alone never decides it: the tag is the carrier.
    assert _terminal_error_meta(e) is None
    e.usage_limit = True
    assert _terminal_error_meta(e) == {"kind": USAGE_LIMIT_KIND}


def test_terminal_error_meta_ranks_usage_limit_below_the_other_verdicts():
    from kiro_crew.dashboard.chat_runner import _terminal_error_meta
    from kiro_crew.dashboard.chat_utils import AUTH_REQUIRED_KIND

    # Entitlement first: its fix (pick a served model) is what the prose says.
    e = _exc("auto", ["gpt-5.6-sol"])
    e.usage_limit = True
    assert _terminal_error_meta(e) == {"kind": MODEL_UNENTITLED_KIND}
    # Then sign-in: the two are mutually exclusive at raise time (see
    # _raise_acp_error), so this only pins the order should that ever change.
    e = AcpError("x", transient=False)
    e.auth_required = True
    e.usage_limit = True
    assert _terminal_error_meta(e) == {"kind": AUTH_REQUIRED_KIND}


def test_raise_acp_error_tags_a_spent_allowance_from_the_raw_frame():
    import pytest

    from kiro_crew.acp.client import _raise_acp_error

    # The shapes a capped account actually produces: kiro-cli's stream envelope
    # around the provider sentence (the reporter's screenshot, request id
    # included), the same sentence bare, and the named exception classes.
    for frame in (
        {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "Encountered an error in the response stream: The monthly usage "
                "limit has been reached (request_id: be06fbe8-3341-4c00-9cac-ff34774e1ec4)"
            ),
        },
        {"code": -32603, "message": "Monthly usage limit has been reached", "data": ""},
        {"code": -32603, "message": "request failed", "data": "FreeTierLimitExceeded"},
    ):
        with pytest.raises(AcpError) as excinfo:
            _raise_acp_error(frame)
        assert excinfo.value.usage_limit is True, frame
        assert excinfo.value.transient is False, frame
        # A spent allowance is terminal but NOT structural: a fresh context can
        # succeed once it resets, so the loop-stopping tag stays off.
        assert excinfo.value.structural_terminal is False, frame
    # A throttle, a 5xx and a sign-in wall are not a spent allowance.
    for frame in (
        {"code": -32603, "message": "ThrottlingException: Rate exceeded", "data": ""},
        {"code": -32603, "message": "HTTP 503 Service Unavailable", "data": ""},
        {"code": -32603, "message": "request failed", "data": "HTTP 401 Unauthorized"},
        {"code": -32603, "message": "Internal error", "data": "Improperly formed request"},
    ):
        with pytest.raises(AcpError) as excinfo:
            _raise_acp_error(frame)
        assert excinfo.value.usage_limit is False, frame


def test_usage_limit_tag_never_rides_with_the_sign_in_tag():
    """A limit answer that happens to carry a 401/403 is the plan's wall, not a
    sign-in wall: `_raise_acp_error` already withholds `auth_required` for it,
    and the usage tag is what that row carries instead."""
    import pytest

    from kiro_crew.acp.client import _raise_acp_error
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    frame = {
        "code": -32603,
        "message": "HTTP 403 Forbidden",
        "data": "The monthly usage limit has been reached",
    }
    with pytest.raises(AcpError) as excinfo:
        _raise_acp_error(frame, backend=ACP_BACKEND_KAS)
    assert excinfo.value.usage_limit is True
    assert excinfo.value.auth_required is False


def test_auth_required_tag_is_reserved_for_host_auth_callback_backends():
    """Harness parity H6: the Kiro sign-in card fixes a sign-in only for a
    harness that authenticates through Crew's identity. The same 401 from a
    harness with its own credential store must stay a plain error, or the card
    would sign in the wrong thing while that harness stays signed out."""
    import pytest

    from kiro_crew.acp.client import AcpAuthRequired, _raise_acp_error
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_HOST_AUTH_CALLBACK, ACP_BACKENDS_KNOWN

    frame = {"code": -32603, "message": "request failed", "data": "HTTP 401 Unauthorized"}
    for backend in ACP_BACKENDS_KNOWN:
        expected = backend in ACP_BACKENDS_HOST_AUTH_CALLBACK
        with pytest.raises(AcpError) as excinfo:
            _raise_acp_error(frame, backend=backend)
        assert getattr(excinfo.value, "auth_required", False) is expected, backend
        assert AcpAuthRequired("signed out", backend=backend).auth_required is expected, backend
    # No backend in reach: never offer the card on a guess.
    with pytest.raises(AcpError) as excinfo:
        _raise_acp_error(frame)
    assert getattr(excinfo.value, "auth_required", False) is False
    assert AcpAuthRequired("signed out").auth_required is False
