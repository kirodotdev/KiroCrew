"""A loopback stand-in for the ``lambda-microvms`` control plane.

moto has no MicroVM backend -- ``RunMicrovm`` against it is a 404 from Flask -- so
the control plane is the one layer of this lane's harness that has to be written.
It is cheap, because the SHAPES are not ours to invent: the real client validates
them before the request leaves, and :mod:`test_cloud_microvm_contract` pins this
fake's routes against botocore's installed model. So this file only has to behave
like the service, not describe it.

Three behaviours here are the service's real contract and not shortcuts:

``RunMicrovm`` answers ``PENDING`` immediately.
    The documentation says a MicroVM starts in ``PENDING`` and transitions to
    ``RUNNING`` once provisioning completes. A fake that did the provisioning
    INSIDE the request takes seconds, botocore gives up and retries, and one
    logical ``RunMicrovm`` creates three containers -- each of them billing on the
    real service. Measured in the spike that preceded this file.

``clientToken`` makes a repeat idempotent.
    Same token, same ``microvmId``, nothing new launched.

An unknown id is ``ResourceNotFoundException`` with a 404.
    Which is what ``api.microvm_status`` reads to tell "the platform has forgotten
    this VM" from "the platform remembers a dead one".

Every verb is backed by the docker primitive in
:mod:`test.microvm_harness.local_engine`, so a crew this fake launches is the
published crew image answering on a loopback port -- which is what makes the
crew's own turn real rather than simulated.
"""

from __future__ import annotations

import json
import socket
import socketserver
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .local_engine import LocalLaunchEngine

_API_PREFIX = "/2025-09-09/microvms"


class _LoopbackServer(ThreadingHTTPServer):
    """A loopback server that does not resolve its own hostname when it binds.

    ``HTTPServer.server_bind`` calls ``socket.getfqdn`` to fill ``server_name``.
    On a machine whose resolver is slow or unreachable that stalls while the socket
    is already bound, so the harness looks hung at startup with no error -- and this
    host's link-local metadata endpoint is blocked rather than refused, which is
    exactly the shape that stalls. Nothing here reads ``server_name``.

    ``test_http_server_bind_ratchet.py`` requires this subclass rather than a bare
    construction, and counts the bare ones.
    """

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


def free_port() -> int:
    """A loopback port nothing is listening on right now.

    Bound and released rather than guessed, because a fixed port collides with
    whatever else this host is running -- and on a dev box that is usually another
    pod.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class FakeVm:
    microvm_id: str
    tag: str
    state: str
    host_port: int
    started_at: str
    client_token: str = ""
    maximum_duration_in_seconds: int = 900
    image_arn: str = "arn:aws:lambda:us-east-1:123456789012:microvm-image/kirocrew"
    image_version: str = "1"
    run_hook_payload: str = ""
    #: Read out of the run payload, so a replacement VM's container carries the
    #: generation the launcher asked for. Without it the readiness generation
    #: clause has only one value to compare and cannot be exercised at all.
    generation: int = 1

    def to_json(self) -> dict[str, Any]:
        return {
            "microvmId": self.microvm_id,
            "state": self.state,
            # The real endpoint is a public HTTPS hostname. This one is a loopback
            # port, which is the single biggest thing the harness cannot reproduce:
            # nothing local has a public hostname, TLS, or a network connector.
            "endpoint": f"http://127.0.0.1:{self.host_port}/",
            "imageArn": self.image_arn,
            "imageVersion": self.image_version,
            "maximumDurationInSeconds": self.maximum_duration_in_seconds,
            "startedAt": self.started_at,
        }


@dataclass
class FakeControlPlane:
    """The state the fake holds, and the call log a test asserts against."""

    engine: LocalLaunchEngine
    vms: dict[str, FakeVm] = field(default_factory=dict)
    by_token: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    _counter: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def next_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"mvm-{self._counter:08x}"

    def run(self, body: dict[str, Any]) -> FakeVm:
        token = str(body.get("clientToken", "") or "")
        if token and token in self.by_token:
            # Idempotency. The same logical launch delivered twice is ONE VM.
            return self.vms[self.by_token[token]]
        microvm_id = self.next_id()
        payload = _read_payload(body.get("runHookPayload", ""))
        tag = str(payload.get("tag", "")) or microvm_id
        generation = payload.get("gen")
        # 0 means "let docker choose", and the engine reads the answer back from
        # the started container. The fake does not pick a port it would then have
        # to hand over, because the handover is the window another listener takes
        # it in.
        port = 0
        vm = FakeVm(
            microvm_id=microvm_id,
            tag=tag,
            state="PENDING",
            host_port=port,
            started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            client_token=token,
            maximum_duration_in_seconds=int(body.get("maximumDurationInSeconds", 900)),
            image_version=str(body.get("imageVersion", "1")),
            run_hook_payload=str(body.get("runHookPayload", "")),
            generation=generation if isinstance(generation, int) else 1,
        )
        self.vms[microvm_id] = vm
        if token:
            self.by_token[token] = microvm_id
        # Provisioning happens OFF the request thread, which is the whole point:
        # a synchronous launch exceeds the client's patience and the retry creates
        # a second VM.
        threading.Thread(target=self._provision, args=(vm,), daemon=True).start()
        return vm

    def _provision(self, vm: FakeVm) -> None:
        try:
            crew = self.engine.launch(vm.tag, host_port=vm.host_port, generation=vm.generation)
            # The port is learned from the started container, and recorded BEFORE
            # the state goes RUNNING. A caller reads the endpoint off a RUNNING
            # VM, so a port written after that transition is a window where the
            # endpoint names port 0.
            vm.host_port = crew.host_port
            vm.state = "RUNNING"
        except Exception:  # noqa: BLE001 - a failed provision is a real platform state
            vm.state = "TERMINATED"

    def terminate(self, vm: FakeVm) -> None:
        self.engine.terminate(vm.tag)
        vm.state = "TERMINATED"

    def reap(self) -> list[str]:
        return self.engine.reap()


def _read_payload(payload: object) -> dict[str, Any]:
    """The run-hook payload as the guest reads it, or ``{}``.

    The fake reads the payload the launcher wrote, which is one more place the two
    shapes are pinned together: a payload the launcher can write and nothing can
    read is a VM that boots, registers nothing, and bills for eight hours.
    """
    if not isinstance(payload, str) or not payload:
        return {}
    try:
        data = json.loads(payload)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class _Handler(BaseHTTPRequestHandler):
    plane: FakeControlPlane

    # Quiet: the harness writes its own log and the default handler logs every
    # request to stderr, which buries the test output it is run from.
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's own spelling
        path = self.path.split("?")[0]
        body = self._read_body()
        self.plane.calls.append(("POST", path))
        if path == _API_PREFIX:
            vm = self.plane.run(body)
            return self._json(200, vm.to_json())
        if path.endswith("/auth-token"):
            vm = self._lookup(path, "/auth-token")
            if vm is None:
                return
            # A shaped PLACEHOLDER. The real token is signed material bound to the
            # endpoint host and scoped to a port list the service enforces; nothing
            # local can produce or check one, so this proves only that the lane
            # parses the response.
            return self._json(200, {"authToken": {"token": f"fake-{vm.microvm_id}"}})
        self._error(404, "ResourceNotFoundException", "no such route")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        self.plane.calls.append(("GET", path))
        if path == _API_PREFIX:
            items = [vm.to_json() for vm in self.plane.vms.values()]
            return self._json(200, {"items": items})
        vm = self._lookup(path, "")
        if vm is None:
            return
        self._json(200, vm.to_json())

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        self.plane.calls.append(("DELETE", path))
        vm = self._lookup(path, "")
        if vm is None:
            return
        self.plane.terminate(vm)
        self._json(200, {})

    # ── internals ────────────────────────────────────────────────────────────

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except ValueError:
            return {}

    def _lookup(self, path: str, suffix: str) -> Optional[FakeVm]:
        trimmed = path[: -len(suffix)] if suffix else path
        microvm_id = trimmed.rsplit("/", 1)[-1]
        vm = self.plane.vms.get(microvm_id)
        if vm is None:
            self._error(404, "ResourceNotFoundException", f"no MicroVM {microvm_id}")
            return None
        return vm

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status: int, code: str, message: str) -> None:
        raw = json.dumps({"message": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        # The header the client maps to a typed exception. Without it a 404 is an
        # untyped error and the lane's "has the platform forgotten this VM" branch
        # cannot fire.
        self.send_header("x-amzn-ErrorType", code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FakeMicroVmEndpoint:
    """The fake as a context manager. ``endpoint_url`` is what the lane is pointed at."""

    def __init__(self, engine: Optional[LocalLaunchEngine] = None) -> None:
        self.plane = FakeControlPlane(engine=engine or LocalLaunchEngine())
        handler = type("_Bound", (_Handler,), {"plane": self.plane})
        # Port 0, and the port read back from the socket that is already bound.
        # One atomic allocation: reserving a port first and binding it afterwards
        # releases it in between, and another listener on the machine can take it
        # in that window -- which shows up as an address-in-use at startup rather
        # than as anything about the test.
        self._server = _LoopbackServer(("127.0.0.1", 0), handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def endpoint_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "FakeMicroVmEndpoint":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Stop serving and remove every container this fake started.

        Both, in that order. A harness that left containers behind on a dev box is
        how a machine ends up with a gigabyte of stopped crews nobody can account
        for.
        """
        self._server.shutdown()
        self._server.server_close()
        self.plane.reap()
