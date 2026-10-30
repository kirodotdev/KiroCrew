"""A fake Chromium that speaks DevTools over the inherited pipe, for tests.

``dashboard_verify`` starts a browser with the DevTools pipe on descriptors 3 and 4, so
the only way to test the spawn, the framing, the request filter, the timeouts and the
kill without a real browser is an executable that speaks the same protocol. This one
answers the handful of commands the runner sends, serves the page the runner fulfils,
plays back a scenario of events, and records what it was handed (its argv, its
environment, any descriptor it inherited beyond the pipe, the document it was given and
the commands it received) so a test can say how the browser was started and what it ran.

The scenario is a JSON file beside the executable, because the runner starts the browser
on a scrubbed environment and an environment variable would not reach it.
"""

from __future__ import annotations

import json
import stat
import sys
import textwrap
from pathlib import Path
from typing import Any

FAKE_BROWSER_SOURCE = textwrap.dedent("""\
    import base64, json, os, sys, threading, time

    HERE = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(HERE, "scenario.json")) as fh:
        SCENARIO = json.load(fh)
    URL = "https://dashboard.invalid/page"


    def record(kind, **fields):
        with open(os.path.join(HERE, "received.jsonl"), "a") as fh:
            fh.write(json.dumps({"kind": kind, **fields}) + "\\n")


    leaked = []
    for fd in range(5, 64):
        try:
            os.fstat(fd)
            leaked.append(fd)
        except OSError:
            pass
    record("start", argv=sys.argv[1:], env=dict(os.environ), pid=os.getpid(), leaked_fds=leaked,
           cwd=os.getcwd())

    def watch_for_a_poke():
        # A test that moves the runner's clock has to make the runner look at it: the runner
        # is waiting on this pipe, so the browser says something it has no use for.
        path = os.path.join(HERE, "poke")
        while True:
            if os.path.exists(path):
                os.unlink(path)
                os.write(4, json.dumps({"method": "Poke.noop", "params": {}}).encode() + b"\\0")
            time.sleep(0.02)


    threading.Thread(target=watch_for_a_poke, daemon=True).start()

    if SCENARIO["mode"] == "exit_at_start":
        sys.stderr.write(SCENARIO["exit_message"] + "\\n")
        sys.exit(1)
    if SCENARIO["mode"] == "exit_leaving_child":
        # The browser's main process dies while a child of it, in its group, lives on.
        import subprocess
        child = subprocess.Popen(["sleep", "120"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        record("child", pid=child.pid)
        sys.stderr.write(SCENARIO["exit_message"] + "\\n")
        sys.exit(1)
    if SCENARIO["mode"] == "hang":
        time.sleep(120)

    buffer = b""


    def read_message():
        global buffer
        while b"\\0" not in buffer:
            chunk = os.read(3, 1 << 20)
            if not chunk:
                return None
            buffer += chunk
        raw, buffer = buffer.split(b"\\0", 1)
        return json.loads(raw)


    def send(message):
        os.write(4, json.dumps(message).encode() + b"\\0")


    def event(method, params):
        send({"method": method, "params": params, "sessionId": "S"})


    def locate(document, needle):
        index = document.index(needle)
        return document.count("\\n", 0, index), index - (document.rfind("\\n", 0, index) + 1)


    document = ""
    while True:
        message = read_message()
        if message is None:
            break
        method = message["method"]
        if method == "Target.createTarget":
            send({"id": message["id"], "result": {"targetId": "T"}})
        elif method == "Target.attachToTarget":
            send({"id": message["id"], "result": {"sessionId": "S"}})
        elif method == "Page.navigate":
            send({"method": "Fetch.requestPaused", "sessionId": "S", "params": {
                "requestId": "doc", "request": {"url": URL}, "resourceType": "Document"}})
            while True:
                nxt = read_message()
                if nxt["method"] == "Fetch.fulfillRequest":
                    params = nxt["params"]
                    document = base64.b64decode(params["body"]).decode("utf-8")
                    record("fulfill", headers=params["responseHeaders"], code=params["responseCode"],
                           document=document)
                    break
            commit = {"frame": {"id": "F", "loaderId": "L", "url": URL, "mimeType": "text/html"},
                      "type": "Navigation"}
            if SCENARIO.get("hold"):
                # The page is "running": nothing more happens until the test releases it.
                while not os.path.exists(os.path.join(HERE, "release")):
                    time.sleep(0.02)
            if SCENARIO.get("load_first"):
                # The document's own load reaches the pipe before the navigate reply.
                event("Page.frameNavigated", commit)
                event("Page.loadEventFired", {"timestamp": 2})
            send({"id": message["id"], "result": {"frameId": "F", "loaderId": "L"}})
            if not SCENARIO.get("load_first"):
                event("Page.frameNavigated", commit)
            for spec in SCENARIO.get("events", []):
                kind = spec["type"]
                if kind == "exception":
                    if spec.get("if_present") and spec["find"] not in document:
                        # A page that breaks only on some text: no such text, no error.
                        continue
                    line, column = locate(document, spec["find"])
                    frame = {"functionName": "", "scriptId": "1", "url": URL,
                             "lineNumber": line, "columnNumber": column}
                    event("Runtime.exceptionThrown", {"timestamp": 1, "exceptionDetails": {
                        "exceptionId": 1, "text": "Uncaught", "lineNumber": line,
                        "columnNumber": column, "url": URL, "stackTrace": {"callFrames": [frame]},
                        "exception": {"type": "object", "description": spec["description"]}}})
                elif kind == "request":
                    send({"method": "Fetch.requestPaused", "sessionId": "S", "params": {
                        "requestId": spec["id"], "request": {"url": spec["url"]},
                        "resourceType": "Script"}})
                elif kind == "crash":
                    event("Inspector.targetCrashed", {})
                elif kind == "huge":
                    event("Runtime.consoleAPICalled", {"type": "error", "args": [
                        {"type": "string", "value": "x" * spec["bytes"]}]})
            if SCENARIO.get("load", True) and not SCENARIO.get("load_first"):
                event("Page.loadEventFired", {"timestamp": 2})
        elif method == "Page.captureScreenshot":
            # The scenario's pictures in order, the last one repeated; an error when the
            # scenario says so, and nothing at all when it holds the browser silent.
            record("command", method=method, params=message.get("params"))
            if SCENARIO.get("screenshot_error"):
                send({"id": message["id"], "error": {"code": -32000, "message": "no surface"}})
            elif SCENARIO.get("screenshot_silent"):
                pass
            else:
                shots = SCENARIO.get("screenshots") or [""]
                taken = SCENARIO.setdefault("_taken", 0)
                SCENARIO["_taken"] = taken + 1
                send({"id": message["id"], "result": {"data": shots[min(taken, len(shots) - 1)]}})
        else:
            record("command", method=method, params=message.get("params"))
            send({"id": message["id"], "result": {}})
    """)


class FakeBrowser:
    """A fake browser on disk, its scenario, and what it received."""

    def __init__(self, directory: Path, **scenario: Any) -> None:
        scenario.setdefault("mode", "normal")
        self.directory = directory
        (directory / "scenario.json").write_text(json.dumps(scenario))
        script = directory / "fake_browser.py"
        script.write_text(FAKE_BROWSER_SOURCE)
        self.executable = directory / "chrome"
        self.executable.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        self.executable.chmod(self.executable.stat().st_mode | stat.S_IXUSR)

    def release(self) -> None:
        """Let a browser started with ``hold=True`` carry on."""
        (self.directory / "release").write_text("")

    def poke(self) -> None:
        """Make the browser say something the runner has no use for, so that a runner
        waiting on it looks at its clock again."""
        (self.directory / "poke").write_text("")

    def records(self, kind: str) -> list[dict[str, Any]]:
        path = self.directory / "received.jsonl"
        if not path.exists():
            return []
        # Only whole lines. The browser appends while a test polls this file, and its
        # `start` line carries the whole environment, so a read can land mid-write; the
        # line is read on the next poll, once its newline is down.
        text = path.read_text()
        complete = text[: text.rfind("\n") + 1]
        rows = [json.loads(line) for line in complete.splitlines()]
        return [row for row in rows if row["kind"] == kind]
