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
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass
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

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, wait_until

_HOST = "127.0.0.1"
_APP_CLIENT = "crew-api-contract-tests"
REDIRECT = "http://127.0.0.1:1/callback"  # never opened: the code is read off the redirect
_FORM = "application/x-www-form-urlencoded"
#: The fake backend asks for approval of its tool call and waits for the answer.
_GATED_INPUT = "[[GATED]] a tool call that waits for the owner"


@dataclass(frozen=True)
class Asking:
    """One app's local authorization: what it asked, and the gateway's first answer."""

    client_id: str
    verifier: str
    query: dict[str, str]
    first: Reply

    @property
    def request_id(self) -> str:
        return str(json.loads(self.first.body)["request_id"])


def redirect_query(reply: Reply) -> dict[str, str]:
    """The query of the address *reply* redirects to, one value per name."""
    location = urllib.parse.urlsplit(reply.headers.get("location", ""))
    return {name: values[0] for name, values in urllib.parse.parse_qs(location.query).items()}


class Gateway:
    def __init__(self, handle: Any) -> None:
        self._handle = handle
        # The approval a case raised and the turn that waits on it, until settled.
        self._waiting: tuple[str, threading.Thread] | None = None

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
        asking = self.ask(client_id)
        self._must(asking.first, 202, "GET /oauth/authorize")

        def redeem() -> str:
            approved = self._must(self.ask_again(asking), 302, "GET /oauth/authorize again")
            code = redirect_query(approved).get("code", "")
            redeemed = self._must(self.exchange(asking, code), 200, "POST /oauth/token")
            return str(json.loads(redeemed.body)["access_token"])

        return asking.request_id, redeem

    def ask(self, client_id: str | None = None) -> Asking:
        """An app's first ``GET /oauth/authorize``; an app of its own unless one is named."""
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        query = {
            "response_type": "code",
            "client_id": client_id or f"{_APP_CLIENT}-{uuid.uuid4().hex[:8]}",
            "client_name": "Crew API contract tests",
            "redirect_uri": REDIRECT,
            "scope": "responses:write conversations:read",
            "state": secrets.token_urlsafe(8),
            "code_challenge": challenge.rstrip(b"=").decode(),
            "code_challenge_method": "S256",
        }
        return Asking(query["client_id"], verifier, query, self._authorize(query))

    def _authorize(self, query: dict[str, str]) -> Reply:
        target = f"/oauth/authorize?{urllib.parse.urlencode(query)}"
        return self._http("GET", target, headers={"Accept": JSON})

    def ask_again(self, asking: Asking) -> Reply:
        """The app's next ``GET /oauth/authorize``, with the request id it was given."""
        query = {**asking.query, "request_id": asking.request_id}
        answer = self._authorize(query)
        # The route makes an app wait between two asks and says how long (Retry-After).
        for _ in range(3):
            if answer.status != 429:
                break
            time.sleep(float(answer.headers.get("retry-after", "1")))
            answer = self._authorize(query)
        return answer

    def decide(self, asking: Asking, decision: str) -> None:
        """The owner answers *asking* through the Owner route."""
        self._must(
            self._owner_call(
                "POST",
                f"/v1/crew/owner/grant-requests/{asking.request_id}/decision",
                {"decision": decision},
            ),
            200,
            f"the owner's {decision} of the app",
        )

    def _form(self, target: str, fields: dict[str, str]) -> Reply:
        body = urllib.parse.urlencode(fields).encode()
        return self._http("POST", target, headers={"Content-Type": _FORM}, body=body)

    def exchange(self, asking: Asking, code: str, verifier: str | None = None) -> Reply:
        """``POST /oauth/token`` with *code*, and the app's own verifier unless one is given."""
        return self._form(
            "/oauth/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "client_id": asking.client_id,
                "code_verifier": asking.verifier if verifier is None else verifier,
            },
        )

    def revoke(self, token: str) -> Reply:
        """``POST /oauth/revoke``: the app gives *token* back."""
        return self._form("/oauth/revoke", {"token": token})

    def metadata(self) -> Reply:
        return self._http("GET", "/.well-known/oauth-authorization-server")

    def read_own_grant(self, token: str) -> Reply:
        """``GET /v1/crew/grant`` with *token* as the app's credential."""
        return self._http("GET", "/v1/crew/grant", headers={"Authorization": f"Bearer {token}"})

    @property
    def origin(self) -> str:
        return f"http://{_HOST}:{self._handle.port}"

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

    def conversation(self) -> str:
        """The id of a conversation the owner made, with no turn in it."""
        made = self._must(
            self._owner_call("POST", "/v1/conversations", {}), 200, "POST /v1/conversations"
        )
        return str(json.loads(made.body)["id"])

    def conversation_with_a_turn(self) -> str:
        """The id of a conversation the owner ran one turn in."""
        ran = self._must(
            self._owner_call("POST", "/v1/responses", {"model": self.model, "input": "hello"}),
            200,
            "POST /v1/responses",
        )
        return str(json.loads(ran.body)["conversation"]["id"])

    def approval_state(self, approval_id: str) -> str:
        read = self._must(
            self._owner_call("GET", f"/v1/crew/owner/approvals/{approval_id}"),
            200,
            "GET /v1/crew/owner/approvals/{approval_id}",
        )
        return str(json.loads(read.body)["state"])

    def _waiting_ids(self) -> set[str]:
        listed = self._must(
            self._owner_call("GET", "/v1/crew/owner/approvals"), 200, "GET /v1/crew/owner/approvals"
        )
        return {str(row["id"]) for row in json.loads(listed.body)["data"]}

    def pending_approval(self) -> str:
        """The id of a tool approval that waits for the owner.

        The owner runs a turn whose tool call the gateway holds. The turn's request stays
        open until someone answers, so it is sent from a thread; :meth:`settle` ends it.
        One approval waits at a time: raising the next one settles the last.
        """
        self.settle()
        known = self._waiting_ids()
        payload = {"model": self.model, "input": _GATED_INPUT}
        assert self._owner  # minted here, not in the thread

        def run() -> None:
            # An unanswered turn outlives the connection's timeout; settle() ends it.
            try:
                self._owner_call("POST", "/v1/responses", payload)
            except OSError:
                pass

        turn = threading.Thread(target=run, name="crew-api-contract-gated-turn", daemon=True)
        turn.start()
        raised = wait_until(lambda: self._waiting_ids() - known, describe=self.diagnostics)
        assert len(raised) == 1, f"one turn raised {sorted(raised)}\n{self.diagnostics()}"
        approval_id = raised.pop()
        self._waiting = (approval_id, turn)
        return approval_id

    def decided_approval(self) -> str:
        """The id of a tool approval the owner has allowed, its turn ended."""
        approval_id = self.pending_approval()
        self._must(
            self._owner_call(
                "POST", f"/v1/crew/owner/approvals/{approval_id}/decision", {"decision": "allow"}
            ),
            200,
            "the owner's answer to the approval",
        )
        self.settle()
        return approval_id

    def settle(self) -> None:
        """End the turn of the approval a case raised: deny it if it still waits."""
        if self._waiting is None:
            return
        approval_id, turn = self._waiting
        self._waiting = None
        if self.approval_state(approval_id) == "pending":
            self._must(
                self._owner_call(
                    "POST",
                    f"/v1/crew/owner/approvals/{approval_id}/decision",
                    {"decision": "deny"},
                ),
                200,
                "denying the approval a case left waiting",
            )
        turn.join(FALLBACK_TIMEOUT_SECS)
        assert not turn.is_alive(), f"the gated turn did not end\n{self.diagnostics()}"

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
