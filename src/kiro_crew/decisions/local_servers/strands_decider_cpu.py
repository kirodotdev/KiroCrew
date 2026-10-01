"""Serve Strands Decider 2B on this machine's CPU for Kiro Crew's decision seam.

Runs inside the model's own environment, never the gateway's: it imports torch,
transformers, peft and strands_decider, which Kiro Crew does not depend on. The
checkpoint is the LoRA adapter and head; the Qwen base it adapts is mirrored
beside it in ``base/``. Both were downloaded and verified by the gateway, so
nothing here reaches the network.
"""

import argparse
import os
import sys
import threading

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
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
from strands_decider import modeling  # noqa: E402
from strands_decider.server import create_app  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--weights", required=True)
parser.add_argument("--port", type=int, required=True)
args = parser.parse_args()

# The checkpoint's config names its base by Hub id; the pinned files cannot be
# edited, so the id is replaced with the mirrored copy as the config is read.
BASE_DIR = os.path.join(args.weights, "base")
_from_json = modeling.StrandsDeciderConfig.from_json


def _from_json_local_base(path: str) -> "modeling.StrandsDeciderConfig":
    config = _from_json(path)
    config.base_model = BASE_DIR
    return config


modeling.StrandsDeciderConfig.from_json = staticmethod(_from_json_local_base)  # type: ignore[method-assign]

app = create_app(args.weights, device="cpu", model_name="strands-decider-2b")


@app.get("/kirocrew-attest", include_in_schema=False)
def _attest() -> PlainTextResponse:
    return PlainTextResponse(ATTEST, status_code=200 if ATTEST else 404)


uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
