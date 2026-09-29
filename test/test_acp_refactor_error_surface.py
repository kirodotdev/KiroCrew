"""Characterization of the ACP error surface: formatted text, raised type, and tags.

Pins, byte for byte, what ``_format_acp_error`` / ``_raise_acp_error`` produce for
each provider-failure branch, plus the free-text classifiers that share their
vocabulary (``classify_provider_error``, ``is_auth_failure_output``, the
registration-throttle and sandbox-init detectors) and the compaction-failure
readers. Every expected value is a literal read off the current code, never one
computed with the function under test.

The code is reached only through the ``kiro_crew.acp.client`` facade, so these
tests hold unchanged before and after the definitions move to their owner module.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp import client as acp_client

_RID = "0f1e2d3c-aaaa-bbbb-cccc-000000000001"
_TEN_MODELS = ["m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8", "m9", "m10"]
# IAM has not propagated a just-minted key yet: auth-shaped, but a retry fixes it.
_CRED_PROPAGATION = (
    "UnrecognizedClientException: The security token included in the request is invalid"
)


def _frame(data: str, message: str = "Internal error", code: int = -32603) -> dict:
    return {"code": code, "message": message, "data": data}


def _tags(
    kind: str = "AcpError",
    *,
    transient: bool | None = False,
    structural: bool = False,
    image: bool = False,
    rejected: str | None = None,
    advertised: tuple[str, ...] = (),
    auth: bool = False,
    usage: bool = False,
) -> dict:
    """The raised error's type and tags; defaults are the untagged generic error."""
    return {
        "type": kind,
        "transient": transient,
        "structural_terminal": structural,
        "image_format_unsupported": image,
        "rejected_model": rejected,
        "advertised": list(advertised),
        "auth_required": auth,
        "usage_limit": usage,
    }


# (error, available_models, formatted text, raised type + tags)
_ERROR_ROWS = [
    pytest.param(
        _frame(f"The model 'auto' is not available request_id: {_RID}"),
        ["claude-a", "claude-b"],
        "Your account does not have access to model 'auto' — the automatic model choice is "
        "not available on your account. Available to you: claude-a, claude-b. Pick one of "
        "these in the model picker for this session, and change the default model under "
        "Settings → Chat (agent.model in ~/.kiro/crew/config.json) so new sessions do not "
        f"start on 'auto' again. Retrying will not help. (request_id: {_RID})",
        _tags(rejected="auto", advertised=("claude-a", "claude-b")),
        id="unentitled-auto",
    ),
    pytest.param(
        _frame("The model 'claude-x' is not available"),
        ["auto", "claude-a"],
        "Your account does not have access to model 'claude-x'. Available to you: auto, "
        "claude-a. Pick one of these in the model picker for this session, and change the "
        "default model under Settings → Chat if it is set to 'claude-x' — set agent.model "
        "to 'auto' in ~/.kiro/crew/config.json to let the backend choose a model your plan "
        "includes. Retrying will not help.",
        _tags(rejected="claude-x", advertised=("auto", "claude-a")),
        id="unentitled-pin-auto-served",
    ),
    pytest.param(
        _frame("Invalid model ID: claude-x"),
        _TEN_MODELS,
        "Your account does not have access to model 'claude-x'. Available to you: m1, m2, "
        "m3, m4, m5, m6, m7, m8 (+2 more). Pick one in the model picker for this session, "
        "and change the default model under Settings → Chat (agent.model in "
        "~/.kiro/crew/config.json) if it is set to 'claude-x'. Retrying will not help.",
        _tags(rejected="claude-x", advertised=tuple(_TEN_MODELS)),
        id="unentitled-pin-no-auto-list-capped",
    ),
    pytest.param(
        _frame(
            "Encountered an error in the response stream: You have reached your monthly "
            "usage limit (request_id: abcd-12)"
        ),
        None,
        "You have reached your monthly usage limit. Retrying will not help until the limit "
        "resets. Check your plan's usage allowance, or switch to a model or account tier "
        "with remaining capacity. (request_id: abcd-12)",
        _tags(usage=True),
        id="usage-limit-period-added",
    ),
    pytest.param(
        _frame("You have reached your monthly usage limit!"),
        None,
        "You have reached your monthly usage limit! Retrying will not help until the limit "
        "resets. Check your plan's usage allowance, or switch to a model or account tier "
        "with remaining capacity.",
        _tags(usage=True),
        id="usage-limit-punctuation-kept",
    ),
    pytest.param(
        _frame("", message="monthly usage limit"),
        None,
        " Retrying will not help until the limit resets. Check your plan's usage "
        "allowance, or switch to a model or account tier with remaining capacity.",
        _tags(usage=True),
        id="usage-limit-empty-detail-leading-space",
    ),
    pytest.param(
        _frame("ImageValidationError: bad"),
        None,
        "The model could not process an image in this conversation. Retrying the unchanged "
        "conversation will fail again. If this turn attached the image, remove it or "
        "re-encode it as PNG or JPEG; otherwise start a new conversation so retained image "
        "data is not replayed.",
        _tags(structural=True, image=True),
        id="image-format",
    ),
    pytest.param(
        _frame(f"The model 'claude-a' is not available request_id: {_RID}"),
        ["claude-a", "auto"],
        "Model 'claude-a' is unavailable on the backend right now (capacity throttle or "
        "region rollout). Try: (1) pick a different model in the model picker, (2) set "
        "agent.model to 'auto' in ~/.kiro/crew/config.json, or (3) wait a minute and "
        f"retry. (request_id: {_RID})",
        _tags(transient=True, rejected="claude-a", advertised=("claude-a", "auto")),
        id="model-unavailable-auto-served",
    ),
    pytest.param(
        _frame("The model 'claude-a' is not available"),
        ["claude-a"],
        "Model 'claude-a' is unavailable on the backend right now (capacity throttle or "
        "region rollout). Try: (1) pick a different model in the model picker, or (2) wait "
        "a minute and retry.",
        _tags(transient=True, rejected="claude-a", advertised=("claude-a",)),
        id="model-unavailable-auto-not-served",
    ),
    pytest.param(
        _frame("The model 'claude-a' is not available"),
        None,
        "Model 'claude-a' is unavailable on the backend right now (capacity throttle or "
        "region rollout). Try: (1) pick a different model in the model picker, (2) set "
        "agent.model to 'auto' in ~/.kiro/crew/config.json, or (3) wait a minute and retry.",
        _tags(transient=True, rejected="claude-a"),
        id="model-unavailable-served-list-unknown",
    ),
    pytest.param(
        _frame("The model you’ve selected is temporarily unavailable."),
        ["x"],
        "The selected model is unavailable on the backend right now (capacity throttle or "
        "region rollout). Try: (1) pick a different model in the model picker, or (2) wait "
        "a minute and retry.",
        _tags(transient=True),
        id="model-temporarily-unavailable",
    ),
    pytest.param(
        _frame(f"ThrottlingException: Rate exceeded request_id: {_RID}"),
        None,
        "Bedrock is throttling requests. Try: (1) wait a few seconds and retry, or (2) "
        f"switch to a different model in the picker (e.g. sonnet). (request_id: {_RID})",
        _tags(transient=True),
        id="throttle",
    ),
    pytest.param(
        _frame(_CRED_PROPAGATION),
        None,
        "The AWS credential was rejected as invalid — usually a transient IAM "
        "credential-propagation delay right after a credential is minted, in which case "
        "the same credential works within a few seconds. Retry in a moment; if it keeps "
        "failing the access key itself is invalid (deleted, rotated, or mistyped) — "
        "refresh your AWS credentials.",
        _tags(transient=True),
        id="credential-propagation",
    ),
    pytest.param(
        _frame("AccessDeniedException: denied"),
        None,
        "Bedrock authentication failed. Refresh your AWS credentials (e.g. re-run your "
        "SSO/login or 'aws sso login'), then retry. If the failure persists, check that "
        "the configured AWS profile has Bedrock InvokeModel access.",
        _tags(),
        id="bedrock-auth",
    ),
    pytest.param(
        _frame("HTTP 401 unauthorized"),
        None,
        "Your session has expired. kiro-cli is not logged in. Run `kiro-cli login` in your "
        "terminal, then start a new chat. Retrying or switching models will not help — "
        "this is a sign-in issue, not a backend error.",
        _tags(),
        id="session-expired-kiro-backend",
    ),
    pytest.param(
        _frame("ECONNREFUSED 127.0.0.1:443"),
        None,
        "Could not reach the model backend (connection refused, reset, or timed out). "
        "Retry in a moment. If it keeps happening, check that the backend endpoint is up "
        "and listening.",
        _tags(transient=True),
        id="connection",
    ),
    pytest.param(
        _frame("InternalServerError please try again"),
        None,
        "The model backend hit a transient error (HTTP 5xx). This is usually momentary — "
        "retry in a moment. If it keeps happening, switch to a different model in the "
        "picker.",
        _tags(transient=True),
        id="http-5xx",
    ),
    pytest.param(
        _frame("Kiro failed to generate a response"),
        None,
        "The model failed to generate a response (transient error — the backend call died "
        "before streaming started, usually a momentary capacity blip). Retry in a moment; "
        "if it keeps happening, switch to a different model in the picker.",
        _tags(transient=True),
        id="generate-failed",
    ),
    pytest.param(
        _frame(f"The service failed to process the request request_id: {_RID}"),
        None,
        "The model backend failed to process the request (transient error, usually a "
        "momentary capacity blip). Retry in a moment; if it keeps happening, switch to a "
        f"different model in the picker. (request_id: {_RID})",
        _tags(transient=True),
        id="process-failed",
    ),
    pytest.param(
        _frame("Improperly formed request"),
        None,
        "The request was rejected as malformed. This is a structural problem with the "
        "request, so retrying it as-is will not help. Try `/compact` to shrink and repair "
        "the conversation, or start a new conversation.",
        _tags(structural=True),
        id="malformed-request",
    ),
    pytest.param(
        _frame("A prompt is already in progress"),
        None,
        "I'm still processing a previous request. Please wait a moment and try again; it "
        "clears on its own once the stale turn expires. If it persists, start a new "
        "conversation.",
        _tags("AcpPromptBusy", transient=None),
        id="prompt-busy-in-data",
    ),
    pytest.param(
        _frame("x", message="prompt already in progress"),
        None,
        "prompt already in progress: x",
        _tags("AcpPromptBusy", transient=None),
        id="prompt-busy-in-message-only",
    ),
    pytest.param(
        _frame(
            "Encountered an error in the response stream: something odd (request_id: 12ab)",
            message="Quota rule",
            code=-32000,
        ),
        None,
        "Quota rule: something odd (request_id: 12ab)",
        _tags(),
        id="fallback-keeps-summary-and-unwraps-envelope",
    ),
    pytest.param(
        _frame("something odd"),
        None,
        "something odd",
        _tags(),
        id="fallback-drops-internal-error-boilerplate",
    ),
    pytest.param(
        _frame(""),
        None,
        "Prompt error: {'code': -32603, 'message': 'Internal error', 'data': ''}",
        _tags(),
        id="fallback-raw-dict",
    ),
    pytest.param("boom", None, "Prompt error: boom", _tags(), id="non-dict-error"),
    pytest.param(
        _frame("leak AKIAIOSFODNN7EXAMPLE https://evil.example.com/x?d=secret"),
        None,
        "leak [REDACTED: credential] https://evil.example.com/x?d=secret",
        _tags(),
        id="credential-redacted",
    ),
]


@pytest.mark.parametrize("error, available, expected_text, _expected_tags", _ERROR_ROWS)
def test_format_acp_error_text(error, available, expected_text, _expected_tags):
    assert acp_client._format_acp_error(error, available) == expected_text


@pytest.mark.parametrize("error, available, expected_text, expected_tags", _ERROR_ROWS)
def test_raise_acp_error_type_and_tags(error, available, expected_text, expected_tags):
    with pytest.raises(acp_client.AcpError) as caught:
        acp_client._raise_acp_error(error, available, backend="")
    exc = caught.value
    assert str(exc) == expected_text
    assert {
        "type": type(exc).__name__,
        "transient": exc.transient,
        "structural_terminal": exc.structural_terminal,
        "image_format_unsupported": exc.image_format_unsupported,
        "rejected_model": exc.rejected_model,
        "advertised": exc.advertised,
        "auth_required": exc.auth_required,
        "usage_limit": exc.usage_limit,
    } == expected_tags


@pytest.mark.parametrize(
    "backend, auth_required, sign_in_text",
    [
        pytest.param(
            "kas",
            True,
            "Not signed in to Kiro. Sign in again from Developer → Agent Backend → Kiro "
            "sign-in, or run `kiro-cli login` in your terminal if kiro-cli owns the sign-in, "
            "then start a new chat.",
            id="host-auth-backend-tags-sign-in",
        ),
        pytest.param(
            "",
            False,
            "kiro-cli is not logged in. Run `kiro-cli login` in your terminal, then start a "
            "new chat.",
            id="kiro-backend-leaves-untagged",
        ),
    ],
)
def test_session_expired_sign_in_tag_follows_the_backend(backend, auth_required, sign_in_text):
    with pytest.raises(acp_client.AcpError) as caught:
        acp_client._raise_acp_error(_frame("HTTP 401 session expired"), None, backend=backend)
    assert caught.value.auth_required is auth_required
    assert caught.value.transient is False
    assert str(caught.value) == (
        f"Your session has expired. {sign_in_text} Retrying or switching models will not "
        "help — this is a sign-in issue, not a backend error."
    )


@pytest.mark.parametrize(
    "available, expected",
    [
        (None, "(2) set agent.model to 'auto' in ~/.kiro/crew/config.json, or (3) "),
        ([], "(2) set agent.model to 'auto' in ~/.kiro/crew/config.json, or (3) "),
        (["auto"], "(2) set agent.model to 'auto' in ~/.kiro/crew/config.json, or (3) "),
        (["x"], "or (2) "),
    ],
)
def test_auto_remedy(available, expected):
    assert acp_client._auto_remedy(available) == expected


@pytest.mark.parametrize(
    "haystack, data, expected",
    [
        ("HTTP 401 unauthorized", None, ("session_expired", False, "session expired")),
        ("session has expired", None, ("session_expired", False, "session expired")),
        (
            "bearer token included in the request is invalid",
            None,
            ("session_expired", False, "session expired"),
        ),
        ("ImageValidationError", None, ("unknown", False, "")),
        (
            "Kiro failed to generate a response",
            None,
            ("http_5xx", True, "failed to generate a response"),
        ),
        (
            "failed to process the request",
            None,
            ("http_5xx", True, "failed to process the request"),
        ),
        ("socket hang up", None, ("connection", True, "socket hang up")),
        (
            "header",
            "Kiro failed to generate a response",
            ("http_5xx", True, "failed to generate a response"),
        ),
    ],
)
def test_classify_provider_error(haystack, data, expected):
    verdict = acp_client.classify_provider_error(haystack, data=data)
    assert (verdict.kind, verdict.retryable, verdict.matched) == expected
    assert verdict.terminal is (not expected[1])


# (provider data, retry verdict, classifier kind). The frame's message is the
# -32603 boilerplate, which carries no classifier token of its own.
_SHARED_VOCABULARY_ROWS = [
    ("You have reached your monthly usage limit", False, "usage_limit"),
    ("Improperly formed request", False, "malformed_request"),
    ("The model 'claude-a' is not available", True, "model_unavailable"),
    ("The model you’ve selected is temporarily unavailable.", True, "model_unavailable"),
    ("ThrottlingException: Rate exceeded", True, "throttle"),
    (_CRED_PROPAGATION, True, "credential_propagation"),
    ("AccessDeniedException: denied", False, "auth"),
    ("HTTP 401 unauthorized", False, "session_expired"),
    ("ECONNREFUSED 127.0.0.1:443", True, "connection"),
    ("InternalServerError please try again", True, "http_5xx"),
    ("Kiro failed to generate a response", True, "http_5xx"),
    ("The service failed to process the request", True, "http_5xx"),
    ("ImageValidationError: bad", False, "unknown"),
    ("something odd", False, "unknown"),
]


@pytest.mark.parametrize("data, retryable, kind", _SHARED_VOCABULARY_ROWS)
def test_raw_frame_classifier_and_provider_classifier_share_one_vocabulary(data, retryable, kind):
    frame = _frame(data)
    verdict = acp_client.classify_provider_error(f"{data} {frame['message']}", data=data)
    assert acp_client._is_transient_raw_error(frame) is retryable
    assert (verdict.kind, verdict.retryable) == (kind, retryable)


@pytest.mark.parametrize(
    "haystack, expected",
    [
        ("not logged in", True),
        ('AccessDeniedException: "Invalid token"', True),
        ("the bearer token included in the request is invalid", True),
        ("HTTP 403", True),
        # The credential-propagation carve-out.
        (_CRED_PROPAGATION, False),
        ("all good", False),
    ],
)
def test_is_auth_failure_output(haystack, expected):
    assert acp_client.is_auth_failure_output(haystack) is expected


@pytest.mark.parametrize(
    "haystack, expected",
    [
        (
            "x\nDynamic client registration failed: HTTP 429 Too Many Requests  \ny",
            "Dynamic client registration failed: HTTP 429 Too Many Requests",
        ),
        # Both tokens must sit on ONE line, and "429" alone is not a throttle mark.
        ("registration failed\nHTTP 429", None),
        ("registration failed: exit 429", None),
    ],
)
def test_registration_throttle_line(haystack, expected):
    assert acp_client.registration_throttle_line(haystack) == expected


def test_registration_rate_limited_error():
    err = acp_client.registration_rate_limited_error(
        "kiro-cli exited (code 1)", "Registration failed: HTTP 429"
    )
    assert type(err) is acp_client.AcpRegistrationRateLimited
    assert isinstance(err, acp_client.AcpProcessDied)
    assert err.transient is True
    assert str(err) == (
        "kiro-cli exited (code 1) — dynamic registration was rate-limited by the endpoint "
        "(HTTP 429); this is endpoint throttling, not a crash — retry later. Cause: "
        "Registration failed: HTTP 429"
    )


@pytest.mark.parametrize(
    "haystack, expected",
    [
        ("sandbox initialization failed: x", True),
        ("foo sandbox_apply bar", True),
        ("  sandbox-exec: denied", True),
        ("x sandbox-exec: y", False),
        ("Operation not permitted", False),
    ],
)
def test_is_sandbox_init_failure_output(haystack, expected):
    assert acp_client.is_sandbox_init_failure_output(haystack) is expected


@pytest.mark.parametrize(
    "params, expected",
    [
        ({"error": [{"message": "boom"}]}, "boom"),
        (
            {
                "cause": {"reason": "MODEL_TEMPORARILY_UNAVAILABLE"},
                "userFacingSessionErrorMessage": "Try later",
                "status": "error",
            },
            "Try later",
        ),
        (
            {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"message": "throttled"}}}}}}}}},
            "no reason reported by the agent (raw: {'a': {'b': {'c': {'d': {'e': {'f': "
            "{'g': {'h': {'message': 'throttled'}}}}}}}}})",
        ),
    ],
)
def test_compaction_failure_detail(params, expected):
    assert acp_client.compaction_failure_detail(params) == expected


@pytest.mark.parametrize(
    "params, expected",
    [
        pytest.param({"error": [{"httpStatusCode": 503}]}, True, id="status-in-list"),
        pytest.param(
            {"cause": [{"reason": "MODEL_TEMPORARILY_UNAVAILABLE"}]}, True, id="reason-in-list"
        ),
        pytest.param({"message": 12.5, "reason": "throttled"}, True, id="non-str-leaf-skipped"),
        pytest.param({"httpStatusCode": True}, False, id="bool-is-not-a-status"),
        pytest.param({"error": [{"status": 503}]}, False, id="status-only-under-its-own-key"),
        pytest.param({"message": 12.5}, False, id="non-str-message"),
        pytest.param({"conversationSummary": "timeout"}, False, id="summary-not-scanned"),
        pytest.param(
            {"a": {"b": {"c": {"d": {"e": {"f": {"message": "throttled"}}}}}}},
            True,
            id="depth-6-read",
        ),
        pytest.param(
            {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"message": "throttled"}}}}}}}},
            False,
            id="depth-7-bounded",
        ),
    ],
)
def test_compaction_failure_is_transient(params, expected):
    assert acp_client.compaction_failure_is_transient(params) is expected


def test_permission_needed_attributes():
    exc = acp_client.AcpPermissionNeeded("p", "so far")
    assert isinstance(exc, acp_client.AcpError)
    assert str(exc) == "Permission needed"
    assert exc.prompt == "p"
    assert exc.response_so_far == "so far"
    assert exc.transient is None
    assert acp_client.AcpPermissionNeeded("p").response_so_far == ""
