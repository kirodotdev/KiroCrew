"""Serve Laya on this machine's CPU for Kiro Crew's decision seam.

Runs inside the model's own environment, never the gateway's: it imports torch
and laya, which Kiro Crew does not depend on. The weights are a directory the
gateway already downloaded and verified, so nothing here reaches the network.
"""

import argparse
import os
import sys
import threading

os.environ.setdefault("HF_HUB_OFFLINE", "1")
# The gateway counts this server ready only once it echoes this secret, so a
# program that took the port while the weights loaded is never mistaken for it.
ATTEST = os.environ.pop("KIROCREW_LOCAL_ATTEST", "")


def _exit_when_gateway_lets_go() -> None:
    # The gateway holds this process's stdin open for as long as it wants the
    # server. End of input -- a stop, or the gateway exiting -- ends the server,
    # so it never outlives the process that started it.
    sys.stdin.buffer.read()
    os._exit(0)


threading.Thread(target=_exit_when_gateway_lets_go, daemon=True).start()

import uvicorn  # noqa: E402
from fastapi.responses import PlainTextResponse  # noqa: E402
from laya.router import Router  # noqa: E402
from laya.serve import create_app  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--weights", required=True)
parser.add_argument("--port", type=int, required=True)
args = parser.parse_args()

# Only the English checkpoint is downloaded; a Router naming the others would
# reach for the network the first time a request routed to one.
router = Router(models={"english": args.weights}, device="cpu", max_loaded=1)
router.preload(["english"])
app = create_app(router)


@app.get("/kirocrew-attest", include_in_schema=False)
def _attest() -> PlainTextResponse:
    return PlainTextResponse(ATTEST, status_code=200 if ATTEST else 404)


uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
