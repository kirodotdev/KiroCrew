"""Serve Plumb-4B on this machine's CPU for Kiro Crew's decision seam.

Runs inside the model's own environment, never the gateway's: it imports torch
and jevk5, which Kiro Crew does not depend on. The weights are a directory the
gateway already downloaded and verified, so nothing here reaches the network.
"""

import argparse
import io
import os
import socketserver
import sys
import threading
import time
from http.server import ThreadingHTTPServer

os.environ.setdefault("HF_HUB_OFFLINE", "1")


def _torch_threads() -> int:
    # Half the CPUs this process may run on, at most 16: one inference keeps the
    # model fast while the owner's sessions keep the other half of the machine.
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    return max(1, min(16, (cpus or 2) // 2))


TORCH_THREADS = _torch_threads()
# Read by the OpenMP runtime when torch loads, so it must be set before the import.
os.environ.setdefault("OMP_NUM_THREADS", str(TORCH_THREADS))
os.environ.setdefault("MKL_NUM_THREADS", str(TORCH_THREADS))
# How long a decision waits for the one in progress before it is refused. The
# client gives up after a few seconds and one CPU inference can take far longer,
# so a request queued longer than this would be answered after its caller left.
QUEUE_WAIT_SECS = 1.0
DECIDE_PATH = "/v1/systemone"
# Largest decision body accepted. The gateway's largest menu is a few tens of KB.
_BODY_MAX_BYTES = 1 << 20
# How long a decision's whole body may take to arrive. It is read before the
# decision waits for the one in progress, so a client that stops sending, or
# trickles, never holds the gate. Also the socket timeout of every connection.
BODY_WAIT_SECS = 10.0
# The gateway counts this server ready only once it echoes this secret, so a
# program that took the port while the weights loaded is never mistaken for it.
ATTEST = os.environ.pop("KIROCREW_LOCAL_ATTEST", "").encode()


def _wait_for_end_of_input() -> None:
    # On Windows stdin is a synchronous pipe, and a read left pending on it makes
    # other queries of that pipe wait behind it. The start-up code of numpy's
    # OpenBLAS DLL is one of them, so the model imports would never return. There
    # the pipe is polled until the gateway closes its end; no read is left
    # pending. A stdin that is not a pipe is read to its end as elsewhere.
    if sys.platform == "win32":
        import msvcrt
        import time

        import _winapi

        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        while True:
            try:
                _winapi.PeekNamedPipe(handle, 0)
            except BrokenPipeError:
                return
            except OSError:
                break
            time.sleep(0.5)
    sys.stdin.buffer.read()


def _exit_when_gateway_lets_go() -> None:
    # The gateway holds this process's stdin open for as long as it wants the
    # server. End of input -- a stop, or the gateway exiting -- ends the server,
    # so it never outlives the process that started it.
    _wait_for_end_of_input()
    os._exit(0)


threading.Thread(target=_exit_when_gateway_lets_go, daemon=True).start()


def _one_at_a_time(handler: type, wait_secs: float) -> type:
    """Subclass *handler* so it runs one decision request at a time.

    jevk5's handler serves each connection on its own thread and waits on its
    model lock with no limit, so requests queue behind a long inference and each
    is still computed to the end after its client has given up. The body is read
    first, within ``BODY_WAIT_SECS``; a decision that then cannot start within
    *wait_secs* is answered 503 at once, and the gate records it as a provider
    error and the caller falls back as it does for any refused decision. Other
    routes are not gated.
    """
    gate = threading.Lock()

    class OneAtATime(handler):  # type: ignore[misc,valid-type]
        # Applied to the connection's socket before any request line is read.
        timeout = BODY_WAIT_SECS

        def do_POST(self) -> None:
            if self.path.rstrip("/") != DECIDE_PATH:
                super().do_POST()
                return
            body = self._read_body()
            if body is None:
                return
            # jevk5 reads the body by Content-Length; it now comes from memory.
            self.rfile = io.BytesIO(body)
            if not gate.acquire(timeout=wait_secs):
                self._reply(503, b'{"error": "busy: another decision is running"}')
                return
            try:
                super().do_POST()
            finally:
                gate.release()

        def _read_body(self) -> bytes | None:
            # The whole body by one deadline, or None once a reply (or nothing,
            # for a client that stopped sending) has been given.
            self.close_connection = True
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = -1
            if length < 0 or length > _BODY_MAX_BYTES:
                self._reply(413, b'{"error": "request body too large or unsized"}')
                return None
            deadline = time.monotonic() + BODY_WAIT_SECS
            chunks: list[bytes] = []
            remaining = length
            try:
                while remaining > 0:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return None
                    # Each read waits only for what is left of the one deadline.
                    self.connection.settimeout(left)
                    chunk = self.rfile.read1(min(remaining, 65536))
                    if not chunk:
                        return None
                    chunks.append(chunk)
                    remaining -= len(chunk)
            except OSError:
                # The client stalled or went away mid-body; nobody is reading.
                return None
            if time.monotonic() > deadline:
                return None
            self.connection.settimeout(BODY_WAIT_SECS)
            return b"".join(chunks)

        def _reply(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

    return OneAtATime


import torch  # noqa: E402
from jevk5.runtime import JevK5  # noqa: E402
from jevk5.server import make_handler  # noqa: E402

torch.set_num_threads(TORCH_THREADS)
torch.set_num_interop_threads(1)

parser = argparse.ArgumentParser()
parser.add_argument("--weights", required=True)
parser.add_argument("--port", type=int, required=True)
args = parser.parse_args()

model = JevK5(args.weights, device="cpu", dtype=torch.bfloat16, graphs=False)
Handler = _one_at_a_time(make_handler(model, "plumb-4b"), QUEUE_WAIT_SECS)


class AttestedHandler(Handler):  # type: ignore[misc,valid-type]
    def do_GET(self) -> None:
        if self.path == "/kirocrew-attest" and ATTEST:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(ATTEST)))
            self.end_headers()
            self.wfile.write(ATTEST)
            return
        super().do_GET()


class LoopbackServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # http.server resolves socket.getfqdn(host) on bind, a system-resolver
        # lookup that can stall for a long time on some macOS hosts while the
        # socket sits bound but not listening. The gateway attests this server
        # by its secret, never by a name, so the lookup buys nothing here.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


LoopbackServer(("127.0.0.1", args.port), AttestedHandler).serve_forever()
