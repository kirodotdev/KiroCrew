"""A fake service for the persistent-sandbox client tests.

A local HTTP server (loopback, port 0) that decodes the CBOR request, records it, and
answers like ``KiroWebBearerService`` does in Q279 section 2: the seven operations,
the summary shapes, and the error table (``__type`` after ``#`` plus ``message``).
Behaviour is scripted per operation with :meth:`FakeBff.queue`; with nothing queued
it keeps a small sandbox table. Nothing here touches the network beyond loopback.
"""

from __future__ import annotations

import http.server
import threading
from collections import defaultdict, deque
from typing import Any

from kiro_crew.managed_env import cbor

PREFIX = "/service/KiroWebBearerService/operation/"
PORTAL_PREFIX = "/service/KiroWebPortalService/operation/"

# Q279 section 2, the error rows the client has to map: (op, status, __type, message).
ERROR_TABLE = {
    "list_401": ("ListSandboxes", 401, "UnauthorizedException", "bad token"),
    "get_404": ("GetSandbox", 404, "ResourceNotFoundException", "no such sandbox"),
    "create_429": ("CreateSandbox", 429, "ServiceQuotaExceededException", "quota"),
    # the service's own cap answer, passed as is by a stand-in with no service mapping.
    "create_402": (
        "CreateSandbox",
        402,
        "ServiceQuotaExceededException",
        "Persistent sandbox limit reached for this instance: at most 10 per instance",
    ),
    "create_429_throttle": ("CreateSandbox", 429, "ThrottlingException", "Rate exceeded"),
    "create_403": ("CreateSandbox", 403, "SandboxUnavailableException", "feature off"),
    "create_400": ("CreateSandbox", 400, "ValidationException", "extraVolumeSizeGiB too large"),
    "stop_409": ("StopSandbox", 409, "ConflictException", "cannot stop yet"),
    "resume_403": ("ResumeSandbox", 403, "ForbiddenException", "creator only"),
    "resume_409_text": (
        "ResumeSandbox",
        409,
        "ConflictException",
        "Sandbox is not in a state that allows this operation",
    ),
    "resume_409_other": ("ResumeSandbox", 409, "ConflictException", "an operator holds it"),
    # the service's own answer right after a Stop, when no service maps it
    "resume_409_service": (
        "ResumeSandbox",
        409,
        "EnvironmentNotReadyException",
        "Persistent environment env-x is being restarted by the host (the host status STOPPING); retry later",
    ),
    "resume_400_kind": ("ResumeSandbox", 400, "EnvironmentNotReadyException", "bad request"),
    "resume_400_text": (
        "ResumeSandbox",
        400,
        "ValidationException",
        "Sandbox is not in a state that allows this operation",
    ),
    "delete_404": ("DeleteSandbox", 404, "ResourceNotFoundException", "gone"),
    "ssh_404": ("StartSshSession", 404, "ResourceNotFoundException", "no such sandbox"),
    "ssh_409": ("StartSshSession", 409, "EnvironmentNotReadyException", "not ready"),
    "any_500": ("GetSandbox", 500, "InternalFailureException", "boom"),
    "any_503": ("GetSandbox", 503, "ServiceUnavailableException", "later"),
    "any_429": ("GetSandbox", 429, "ThrottlingException", "slow down"),
}

DROP = "drop"  # queue this instead of an answer: close the connection without one
# Queue one of these instead of an answer: the operation runs as usual, then its answer is
# lost on the way: a 200 whose body stops after 5 bytes, or a status line that can't be read.
CUT = "cut"
BAD_STATUS = "bad_status"


def cut_error(status: int) -> str:
    """Queue this as the status, with an error body: that answer, its body cut after 5 bytes."""
    return f"{CUT}:{status}"


def err(key: str) -> tuple[int, dict]:
    _, status, typ, msg = ERROR_TABLE[key]
    return status, {"__type": f"com.example.kiro#{typ}", "message": msg}


class FakeBff:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.sandboxes: dict[str, dict] = {}
        self.list_pages: list[dict] | None = None  # explicit pages for ListSandboxes
        self.get_statuses: dict[str, deque] = defaultdict(deque)  # per-id Get answers
        self._queued: dict[str, deque] = defaultdict(deque)
        self._next = 0
        self._lock = threading.Lock()
        self._inflight = 0
        self.max_inflight = 0
        self.drop_update_after_commit = (
            False  # the next UpdateNetworkConfiguration is applied, then the answer is lost
        )
        # the test user instance's network setting (EXP-63); row=False answers without a customDomains member
        self.netcfg: dict = {
            "networkAccess": "INTEGRATIONS_ONLY",
            "customDomains": ["corp.example.com"],
            "row": True,
        }
        self.hold: threading.Event | None = None  # when set, handlers wait on it
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.endpoint = f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # -- scripting -----------------------------------------------------------------

    def queue(self, op: str, status: int | str, body: dict | bytes | None = None) -> None:
        """``body`` as bytes is sent as is (an answer that is not CBOR)."""
        self._queued[op].append((status, body or {}))

    def queue_error(self, key: str, *, op: str | None = None) -> None:
        status, body = err(key)
        self.queue(op or ERROR_TABLE[key][0], status, body)

    def add_sandbox(self, status: str = "RUNNING", name: str = "foreign") -> str:
        self._next += 1
        sid = f"env-{self._next:03d}"
        self.sandboxes[sid] = {
            "execEnvironmentId": sid,
            "status": status,
            "displayName": name,
            "createdAt": 1794420000 + self._next,
        }
        return sid

    def add_other(self, status: str = "RUNNING", name: str = "made-in-the-web-app") -> str:
        """A sandbox another client made (the web app, the IDE, another Crew): ``other-NNN``."""
        n = sum(1 for k in self.sandboxes if k.startswith("other-")) + 1
        sid = f"other-{n:03d}"
        self.sandboxes[sid] = {
            "execEnvironmentId": sid,
            "status": status,
            "displayName": name,
            "createdAt": 1794410000 + n,
        }
        return sid

    def naming(self, sid: str) -> list[dict]:
        """Every recorded request whose body names ``sid`` (at any depth)."""
        return [r for r in self.requests if sid in repr(r["body"])]

    def ops(self) -> list[str]:
        return [r["op"] for r in self.requests]

    def count(self, op: str) -> int:
        return self.ops().count(op)

    # -- behaviour -----------------------------------------------------------------

    def _answer(self, op: str, body: dict) -> tuple[int | str, dict]:
        if self._queued[op]:
            status, out = self._queued[op].popleft()
            if status not in (CUT, BAD_STATUS):
                return status, out
            return status, self._run(op, body)[1]
        return self._run(op, body)

    def _run(self, op: str, body: dict) -> tuple[int | str, dict]:
        ident = body.get("execEnvironmentId") or (body.get("identifier") or {}).get(
            "executionEnvironmentId"
        )
        box = self.sandboxes.get(ident) if ident else None
        if op == "ListSandboxes":
            if self.list_pages is not None:
                idx = 0
                if body.get("nextToken"):
                    idx = int(str(body["nextToken"]).removeprefix("page-"))
                page = dict(self.list_pages[idx])
                if idx + 1 < len(self.list_pages):
                    page["nextToken"] = f"page-{idx + 1}"
                return 200, page
            live = [s for s in self.sandboxes.values() if s["status"] != "DELETED"]
            return 200, {"sandboxes": live}
        if op == "GetNetworkConfiguration":
            cfg = {"networkAccess": self.netcfg["networkAccess"]}
            if self.netcfg["row"]:
                cfg["customDomains"] = list(self.netcfg["customDomains"])
            return 200, {"networkConfig": cfg}
        if op == "UpdateNetworkConfiguration":
            nc = body.get("networkConfig") or {}
            if not body.get("csrfToken") or "networkAccess" not in nc:
                return err("any_500")
            self.netcfg.update(
                networkAccess=nc["networkAccess"],
                customDomains=list(nc.get("customDomains") or []),
                row=True,
            )
            if self.drop_update_after_commit:
                self.drop_update_after_commit = False
                return DROP, {}
            return 200, {
                "networkConfig": {
                    "networkAccess": nc["networkAccess"],
                    "customDomains": list(self.netcfg["customDomains"]),
                }
            }
        if op == "CreateSandbox":
            sid = self.add_sandbox("WAITING", body["displayName"])
            return 200, dict(self.sandboxes[sid])
        if op == "StartSshSession":
            if box is None or box["status"] == "DELETED":
                return err("ssh_404")
            if box["status"] != "RUNNING":
                return err("ssh_409")
            self._next += 1
            return 200, {
                "ssmSessionId": f"ssm-{self._next}",
                "streamUrl": f"wss://stream.invalid/{self._next}-STREAMSECRET",
                "tokenValue": f"mgs-{self._next}-MGSSECRET",
            }
        if box is None or box["status"] == "DELETED":
            return err("get_404") if op != "DeleteSandbox" else err("delete_404")
        if op == "GetSandbox":
            if self.get_statuses[ident]:
                box["status"] = self.get_statuses[ident].popleft()
            return 200, dict(box)
        if op == "StopSandbox":
            box["status"] = "STOPPED"
            return 200, {"execEnvironmentId": ident, "status": "STOPPED"}
        if op == "ResumeSandbox":
            box["status"] = "PENDING"
            return 200, {"execEnvironmentId": ident, "status": "PENDING"}
        if op == "DeleteSandbox":
            box["status"] = "DELETED"
            return 200, {"execEnvironmentId": ident, "status": "DELETED"}
        if op == "UpdateSandbox":
            # As the restart-capable the service: a running sandbox answers RUNNING until its
            # restart begins (script that with get_statuses); a stopped one keeps the value.
            if "idleTimeoutMinutes" in body:
                box["idleTimeoutMinutes"] = body["idleTimeoutMinutes"]
            answer = {
                "execEnvironmentId": ident,
                "status": box["status"],
                "idleTimeoutMinutes": box.get("idleTimeoutMinutes", 60),
            }
            if "displayName" in body:
                # As the service with its sandboxRenameEnabled switch on: applied and echoed.
                box["displayName"] = body["displayName"]
                answer["displayName"] = body["displayName"]
            return 200, answer
        return err("any_500")

    def _handler(self):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # silence
                pass

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                op = self.path[len(PREFIX) :] if self.path.startswith(PREFIX) else self.path
                if self.path.startswith(PORTAL_PREFIX):
                    op = self.path[len(PORTAL_PREFIX) :]
                body = cbor.loads(raw)
                with outer._lock:
                    outer._inflight += 1
                    outer.max_inflight = max(outer.max_inflight, outer._inflight)
                    outer.requests.append(
                        {"op": op, "body": body, "headers": self.headers, "path": self.path}
                    )
                try:
                    if outer.hold is not None:
                        outer.hold.wait(5)
                    with outer._lock:
                        status, out = outer._answer(op, body)
                finally:
                    with outer._lock:
                        outer._inflight -= 1
                cut_to = None
                if status == CUT:
                    cut_to = 200
                elif isinstance(status, str) and status.startswith(CUT + ":"):
                    cut_to = int(status.split(":", 1)[1])
                if cut_to is not None:
                    data = cbor.dumps(out)
                    self.send_response(cut_to)
                    self.send_header("Content-Type", "application/cbor")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data[:5])
                    self.close_connection = True
                    return
                if status == BAD_STATUS:
                    self.wfile.write(b"HTTX/1.1 2OO OK\r\n\r\n")
                    self.close_connection = True
                    return
                if status == DROP:
                    self.close_connection = True
                    self.connection.close()
                    return
                location = out.pop("__location", None) if isinstance(out, dict) else None
                data = out if isinstance(out, bytes) else cbor.dumps(out)
                self.send_response(int(status))
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Type", "application/cbor")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return H


class FakeTime:
    """A clock that only moves when something sleeps (or the test advances it)."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s

    def clock(self) -> float:
        return self.t

    def now(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s
