"""Serve Plumb-4B on this machine's CPU for Kiro Crew's decision seam.

Runs inside the model's own environment, never the gateway's: it imports torch
and jevk5, which Kiro Crew does not depend on. The weights are a directory the
gateway already downloaded and verified, so nothing here reaches the network.
"""

import argparse
import os
import sys
import threading
from http.server import ThreadingHTTPServer

os.environ.setdefault("HF_HUB_OFFLINE", "1")
# The gateway counts this server ready only once it echoes this secret, so a
# program that took the port while the weights loaded is never mistaken for it.
ATTEST = os.environ.pop("KIROCREW_LOCAL_ATTEST", "").encode()


def _exit_when_gateway_lets_go() -> None:
    # The gateway holds this process's stdin open for as long as it wants the
    # server. End of input -- a stop, or the gateway exiting -- ends the server,
    # so it never outlives the process that started it.
    sys.stdin.buffer.read()
    os._exit(0)


threading.Thread(target=_exit_when_gateway_lets_go, daemon=True).start()

import torch  # noqa: E402
from jevk5.runtime import JevK5  # noqa: E402
from jevk5.server import make_handler  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--weights", required=True)
parser.add_argument("--port", type=int, required=True)
args = parser.parse_args()

model = JevK5(args.weights, device="cpu", dtype=torch.bfloat16, graphs=False)
Handler = make_handler(model, "plumb-4b")


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


ThreadingHTTPServer(("127.0.0.1", args.port), AttestedHandler).serve_forever()
