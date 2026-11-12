"""The Crew API's error body, request id and version header, for every ``/v1/`` answer.

A leaf module with no dashboard imports, so token auth can answer a ``/v1/`` refusal
in the same shape as an Owner route does without loading the handlers. The one
exception is ``/v1/chat/completions``, which is frozen and keeps its own refusal.

The error body is ``{"error": {"type", "code", "message", "param", "request_id"}}``.
"""

from __future__ import annotations

import json
import re
import uuid

from aiohttp import web

CREW_API_PATH_PREFIX = "/v1/"
# Frozen: its callers read the refusal it has always given, so it stays outside the
# contract. Matched exactly, the way token auth matches its internal paths.
_FROZEN_PATHS = frozenset({"/v1/chat/completions"})
# Owner routes refuse every app token, so token auth labels an app-scope refusal
# here ``owner_only``, the code the route itself gives.
OWNER_PATH_PREFIX = "/v1/crew/owner"

# shortcut: one fixed server version, and the request's own Crew-API-Version header is
# not read yet. Upgrade when the minor-version format is settled and a second minor
# version exists to negotiate between.
CREW_API_VERSION = "1.0"
VERSION_HEADER = "Crew-API-Version"
REQUEST_ID_HEADER = "X-Request-Id"

_REQUEST_ID_RE = re.compile(r"^req_[A-Za-z0-9_-]{1,64}$")

# shortcut: only the statuses a route answers today (401, 403; 5xx is handled below).
# Any other status gets ``invalid_request_error`` / ``invalid_request``. Add a row
# with the first endpoint that can answer that status.
_ERROR_TYPES = {
    401: "authentication_error",
    403: "permission_error",
}
# The code an error gets when the handler's body names none.
_DEFAULT_CODES = {
    401: "invalid_token",
    403: "owner_only",
}


def is_crew_api_path(path: str) -> bool:
    """Whether *path* is answered under the Crew API's contract."""
    return path.startswith(CREW_API_PATH_PREFIX) and path not in _FROZEN_PATHS


def is_owner_path(path: str) -> bool:
    """Whether *path* is an Owner route, which no app token may call."""
    return path.startswith(OWNER_PATH_PREFIX + "/")


def request_id_for(request: web.Request) -> str:
    """The caller's ``req_``-shaped ``X-Request-Id``, else a new one."""
    supplied = request.headers.get(REQUEST_ID_HEADER, "")
    if _REQUEST_ID_RE.fullmatch(supplied):
        return supplied
    return "req_" + uuid.uuid4().hex


def error_type(status: int) -> str:
    if status >= 500:
        return "server_error"
    return _ERROR_TYPES.get(status, "invalid_request_error")


def default_code(status: int) -> str:
    if status >= 500:
        return "internal_error"
    return _DEFAULT_CODES.get(status, "invalid_request")


def contract_error(
    status: int,
    code: str,
    message: str,
    request_id: str,
    headers: dict[str, str] | None = None,
) -> web.Response:
    """A Crew API error response, with its request id and version headers set."""
    body = {
        "error": {
            "type": error_type(status),
            "code": code,
            "message": message,
            "param": None,
            "request_id": request_id,
        }
    }
    # The code is at ``error.code``. The error-code ratchet
    # (test_error_code_contract.py) reads only a top-level ``code`` on a
    # ``json_response``, so this sink is outside it;
    # test_crew_api_owner.py pins the code on every error instead.
    response = web.Response(
        text=json.dumps(body),
        status=status,
        content_type="application/json",
        headers=headers,
    )
    if status == 401:
        # RFC 9110 requires a challenge on a 401. Never ``Basic``: a browser answers
        # that scheme with its own login dialog.
        response.headers["WWW-Authenticate"] = 'Bearer error="invalid_token"'
    response.headers[REQUEST_ID_HEADER] = request_id
    response.headers[VERSION_HEADER] = CREW_API_VERSION
    return response
