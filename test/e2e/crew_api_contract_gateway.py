"""One booted gateway and the callers a contract case can be: owner, app, nobody.

The gateway is the repo's own real-gateway rig (``e2e.test_gateway_boot_matrix._booted``:
``spawn_feature_gateway`` with the packaged fake ACP backend as its engine), so this
module starts no process of its own.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import http.client
import json
import secrets
import time
import urllib.parse
import uuid
from collections.abc import Callable
from typing import Any

from e2e.crew_api_contract_spec import (
    ANONYMOUS,
    APP,
    INVALID,
    JSON,
    NO_BODY,
    OWNER,
    REQUEST_ID,
    Case,
    Operation,
    Reply,
)

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_HOST = "127.0.0.1"
_APP_CLIENT = "crew-api-contract-tests"
_REDIRECT = "http://127.0.0.1:1/callback"  # never opened: the code is read off the redirect


class Gateway:
    def __init__(self, handle: Any) -> None:
        self._handle = handle

    def diagnostics(self) -> str:
        return str(self._handle.diagnostics())

    def _http(
        self,
        method: str,
        target: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> Reply:
        connection = http.client.HTTPConnection(
            _HOST, self._handle.port, timeout=FALLBACK_TIMEOUT_SECS
        )
        try:
            connection.request(method, target, body=body, headers=headers or {})
            answer = connection.getresponse()
            return Reply(
                answer.status,
                {name.lower(): value for name, value in answer.getheaders()},
                answer.read(),
            )
        finally:
            connection.close()

    def _must(self, reply: Reply, status: int, what: str) -> Reply:
        # The body can hold a credential, so a failed step names its status only.
        assert (
            reply.status == status
        ), f"{what} answered {reply.status}, not {status}\n{self.diagnostics()}"
        return reply

    @functools.cached_property
    def _owner(self) -> str:
        """The owner's session, minted the way ``kirocrew token --bearer`` mints it."""
        secret = (self._handle.home / ".local_secret").read_text(encoding="utf-8").strip()
        minted = self._must(
            self._http("GET", "/api/token/local?kind=session", headers={"X-Local-Secret": secret}),
            200,
            "GET /api/token/local",
        )
        return str(json.loads(minted.body)["token"])

    def _owner_call(self, method: str, target: str, payload: Any = None) -> Reply:
        headers = {"Authorization": f"Bearer {self._owner}"}
        if payload is not None:
            headers["Content-Type"] = JSON
        body = None if payload is None else json.dumps(payload).encode()
        return self._http(method, target, headers=headers, body=body)

    def _ask(self, client_id: str) -> tuple[str, Callable[[], str]]:
        """Start local authorization for *client_id*.

        Returns the pending request's id and a function that redeems it for the app's
        access token once the owner has approved it.
        """
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        asked = {
            "response_type": "code",
            "client_id": client_id,
            "client_name": "Crew API contract tests",
            "redirect_uri": _REDIRECT,
            "scope": "responses:write conversations:read",
            "state": secrets.token_urlsafe(8),
            "code_challenge": challenge.rstrip(b"=").decode(),
            "code_challenge_method": "S256",
        }

        def authorize(**more: str) -> Reply:
            query = urllib.parse.urlencode({**asked, **more})
            return self._http("GET", f"/oauth/authorize?{query}", headers={"Accept": JSON})

        pending = self._must(authorize(), 202, "GET /oauth/authorize")
        request_id = str(json.loads(pending.body)["request_id"])

        def redeem() -> str:
            # The route makes an app wait between two asks and says how long (Retry-After).
            approved = authorize(request_id=request_id)
            for _ in range(3):
                if approved.status != 429:
                    break
                time.sleep(float(approved.headers.get("retry-after", "1")))
                approved = authorize(request_id=request_id)
            self._must(approved, 302, "GET /oauth/authorize again")
            location = urllib.parse.urlsplit(approved.headers.get("location", ""))
            form = {
                "grant_type": "authorization_code",
                "code": urllib.parse.parse_qs(location.query).get("code", [""])[0],
                "redirect_uri": _REDIRECT,
                "client_id": client_id,
                "code_verifier": verifier,
            }
            redeemed = self._must(
                self._http(
                    "POST",
                    "/oauth/token",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    body=urllib.parse.urlencode(form).encode(),
                ),
                200,
                "POST /oauth/token",
            )
            return str(json.loads(redeemed.body)["access_token"])

        return request_id, redeem

    def _grant(self, client_id: str) -> str:
        """An access token for *client_id*: the app asks, the owner approves, the app redeems."""
        request_id, redeem = self._ask(client_id)
        self._must(
            self._owner_call(
                "POST",
                f"/v1/crew/owner/grant-requests/{request_id}/decision",
                {"decision": "approve"},
            ),
            200,
            "the owner's approval of the app",
        )
        return redeem()

    @functools.cached_property
    def _app(self) -> str:
        return self._grant(_APP_CLIENT)

    def app_grant(self) -> None:
        """Make sure the app caller's grant exists."""
        assert self._app

    def pending_grant_request(self) -> str:
        """The id of a grant request no one has answered, from an app of its own."""
        request_id, _ = self._ask(f"{_APP_CLIENT}-{uuid.uuid4().hex[:8]}")
        return request_id

    def answered_grant_request(self) -> str:
        """The id of a grant request the owner has denied, from an app of its own."""
        request_id = self.pending_grant_request()
        self._must(
            self._owner_call(
                "POST",
                f"/v1/crew/owner/grant-requests/{request_id}/decision",
                {"decision": "deny"},
            ),
            200,
            "the owner's denial of the app",
        )
        return request_id

    def revocable_grant(self) -> str:
        """The id of a grant of its own, so revoking it leaves the app caller's alone."""
        client_id = f"{_APP_CLIENT}-{uuid.uuid4().hex[:8]}"
        self._grant(client_id)
        listed = self._must(
            self._owner_call("GET", "/v1/crew/owner/grants"), 200, "GET /v1/crew/owner/grants"
        )
        ids = [
            row["id"] for row in json.loads(listed.body)["data"] if row["client_id"] == client_id
        ]
        assert len(ids) == 1, f"the new grant is not listed once\n{self.diagnostics()}"
        return str(ids[0])

    def kept_response(self) -> str:
        """The id of a turn the owner ran, which the gateway keeps to be read back."""
        ran = self._must(
            self._owner_call("POST", "/v1/responses", {"model": self.model, "input": "hello"}),
            200,
            "POST /v1/responses",
        )
        return str(json.loads(ran.body)["id"])

    def _credential(self, caller: str) -> str | None:
        if caller == ANONYMOUS:
            return None
        if caller == INVALID:
            return "not-a-credential-this-gateway-signed"
        assert caller in (OWNER, APP), caller
        return self._owner if caller == OWNER else self._app

    @functools.cached_property
    def model(self) -> str:
        """An agent the owner can name as ``model``."""
        listed = self._must(
            self._http("GET", "/v1/models", headers={"Authorization": f"Bearer {self._owner}"}),
            200,
            "GET /v1/models",
        )
        agents = [
            row["id"]
            for row in json.loads(listed.body)["data"]
            if row.get("crew", {}).get("kind") == "agent"
        ]
        assert agents, f"the seeded gateway lists no agent\n{self.diagnostics()}"
        return str(agents[0])

    def send(self, operation: Operation, case: Case) -> tuple[Reply, str]:
        """Send *case* to *operation*; the reply and the request id that went with it."""
        if case.before is not None:
            case.before(self)
        params = case.path(self) if callable(case.path) else case.path
        path = operation.path
        for name in _placeholders(path):
            value = urllib.parse.quote(str(params.get(name, "unknown")), safe="")
            path = path.replace("{" + name + "}", value)
        request_id = "req_" + uuid.uuid4().hex
        headers = {REQUEST_ID: request_id}
        credential = self._credential(case.caller)
        if credential is not None:
            headers["Authorization"] = f"Bearer {credential}"
        body = None
        if case.json is not NO_BODY:
            payload = case.json(self) if callable(case.json) else case.json
            body = json.dumps(payload).encode()
            headers["Content-Type"] = JSON
        return self._http(operation.method, path, headers=headers, body=body), request_id


def _placeholders(path: str) -> list[str]:
    return [part[1:-1] for part in path.split("/") if part.startswith("{") and part.endswith("}")]
