"""A fake ``signal-cli jsonRpc`` for the Signal channel's end-to-end tests.

Speaks newline-delimited JSON-RPC 2.0 on stdio like the real one. Behaviour is
driven by environment variables so the test can launch it through the real
``SignalClient`` argv (``<cli> -a <account> jsonRpc``):

* ``FAKE_SIGNAL_MODE=unregistered`` -- print signal-cli's own startup error on
  stderr and exit 1 before reading anything (the "not registered" account).
* ``FAKE_SIGNAL_MODE=silent`` -- log the ``version`` probe (see
  ``FAKE_SIGNAL_LOG``) but never answer it, so the caller's probe hangs until it
  gives up or is cancelled; the child still exits when stdin closes.
* ``FAKE_SIGNAL_NOTIFY`` -- a path to a JSON list of ``receive`` params objects,
  emitted as notifications right after the first ``version`` request is
  answered (the real binary emits them as soon as it is receiving).
* ``FAKE_SIGNAL_LOG`` -- a path every request is appended to as one JSON line,
  written BEFORE the response, so a caller that saw the response can read it.
* ``FAKE_SIGNAL_REPLIES`` -- a path to a JSON list of
  ``{"after": "<substring>", "receive": <params>}``. Right after answering a
  ``send`` whose message contains ``after``, that ``receive`` is emitted, once:
  a person answering what they were just sent (an approval prompt, say).
* ``FAKE_SIGNAL_ECHO_NOTE_TO_SELF=1`` -- after answering a ``noteToSelf`` send,
  emit its echo the way another device on the account re-syncs it: a
  ``syncMessage.sentMessage`` to the account itself carrying the send's own
  timestamp and body.
* ``FAKE_SIGNAL_ATTACHMENTS`` -- a directory standing in for signal-cli's
  ``<data-dir>/attachments``: ``getAttachment`` answers ``{"data": <base64>}``
  for a file named by the requested id, and signal-cli's own "Could not find
  attachment" user error (code -1) for anything else.

``sendReaction`` answers like a send (a timestamp and per-recipient results).

It exits when stdin closes, which is the real binary's documented contract.
Standard library only.
"""

from __future__ import annotations

import base64
import json
import os
import sys


def _write(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _receive(params: dict) -> None:
    _write({"jsonrpc": "2.0", "method": "receive", "params": params})


def _echo(account: str, timestamp: int, body: str) -> dict:
    """``receive`` params re-syncing this account's own Note to Self send."""
    sent = {"destinationNumber": account, "timestamp": timestamp, "message": body}
    envelope = {
        "sourceNumber": account,
        "timestamp": timestamp,
        "syncMessage": {"sentMessage": sent},
    }
    return {"envelope": envelope, "account": account}


def _load(path: str) -> list:
    if not path:
        return []
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _attachment(params: dict) -> dict:
    """``getAttachment``: the stored file's bytes, base64, or signal-cli's -1."""
    store = os.environ.get("FAKE_SIGNAL_ATTACHMENTS", "")
    attachment_id = str(params.get("id") or "")
    path = os.path.join(store, attachment_id) if store else ""
    # signal-cli sanitizes the id to [A-Za-z0-9_.-]; the fake refuses anything else.
    safe = attachment_id and all(c.isalnum() or c in "_.-" for c in attachment_id)
    if not (safe and path and os.path.isfile(path)):
        message = f"Could not find attachment with ID: {attachment_id}"
        return {"error": {"code": -1, "message": message}}
    with open(path, "rb") as fh:
        return {"result": {"data": base64.b64encode(fh.read()).decode("ascii")}}


def main(argv: list[str]) -> int:
    account = argv[argv.index("-a") + 1] if "-a" in argv else ""
    if os.environ.get("FAKE_SIGNAL_MODE") == "unregistered":
        sys.stderr.write(f"User {account} is not registered.\n")
        sys.stderr.flush()
        return 1
    if "jsonRpc" not in argv:
        sys.stderr.write("fake signal-cli: expected the jsonRpc command\n")
        return 2
    log_path = os.environ.get("FAKE_SIGNAL_LOG", "")
    pending = _load(os.environ.get("FAKE_SIGNAL_NOTIFY", ""))
    replies = _load(os.environ.get("FAKE_SIGNAL_REPLIES", ""))
    echo_note_to_self = os.environ.get("FAKE_SIGNAL_ECHO_NOTE_TO_SELF") == "1"
    silent = os.environ.get("FAKE_SIGNAL_MODE") == "silent"
    timestamp = 1700000000000
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        if log_path:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(request) + "\n")
        method = request.get("method")
        req_id = request.get("id")
        if method == "version" and silent:
            continue
        if method == "version":
            _write({"jsonrpc": "2.0", "result": {"version": "0.0.0-fake"}, "id": req_id})
            for params in pending:
                _receive(params)
            pending = []
        elif method == "send":
            timestamp += 1
            _write(
                {"jsonrpc": "2.0", "result": {"timestamp": timestamp, "results": []}, "id": req_id}
            )
            params = request.get("params") or {}
            body = str(params.get("message") or "")
            if echo_note_to_self and params.get("noteToSelf"):
                _receive(_echo(account, timestamp, body))
            for reply in list(replies):
                if reply["after"] in body:
                    replies.remove(reply)
                    _receive(reply["receive"])
        elif method == "sendReaction":
            timestamp += 1
            _write(
                {"jsonrpc": "2.0", "result": {"timestamp": timestamp, "results": []}, "id": req_id}
            )
        elif method == "getAttachment":
            _write({"jsonrpc": "2.0", **_attachment(request.get("params") or {}), "id": req_id})
        elif method in ("sendTyping", "sendReceipt"):
            _write({"jsonrpc": "2.0", "result": {}, "id": req_id})
        else:
            _write(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Method not implemented: {method}"},
                    "id": req_id,
                }
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
