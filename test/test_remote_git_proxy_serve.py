"""The guest git proxy's own process, in process: serve(), its HTTP handler and main().

The end-to-end tests run ``git_proxy.py`` as a subprocess; these drive the same
code with a scripted gateway on a pipe, so each answer shape is checked here.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import os
import queue
import sys
import threading
from pathlib import Path

import pytest

from kiro_crew.remote_session import git_proxy
from kiro_crew.testing.wait import wait_until

SECRET = "ab" * 32
WAIT = 30.0


class _Frames:
    """The proxy's stdout: every frame it sends, in order."""

    def __init__(self) -> None:
        self.sent: "queue.Queue[dict]" = queue.Queue()

    def write(self, text: str) -> None:
        for line in text.splitlines():
            if line.strip():
                self.sent.put(json.loads(line))

    def flush(self) -> None:
        pass

    def next(self) -> dict:
        return self.sent.get(timeout=WAIT)

    def request(self) -> list:
        """One request's frames, through its ``end``."""
        frames = [self.next()]
        while frames[-1]["t"] != "end":
            frames.append(self.next())
        return frames


class _Proxy:
    def __init__(self, tmp_path: Path) -> None:
        read_fd, write_fd = os.pipe()
        self.out = _Frames()
        self._reply = os.fdopen(write_fd, "w", encoding="utf-8")
        self.gateway = git_proxy.Gateway(
            out=self.out, inp=os.fdopen(read_fd, "r", encoding="utf-8")
        )
        self.port_file = tmp_path / "git-proxy.port"
        self.lend = Path(git_proxy.lend_file(str(self.port_file)))
        self.result: list = []
        self.thread = threading.Thread(
            target=lambda: self.result.append(
                git_proxy.serve(self.gateway, port_file=str(self.port_file), secret=SECRET)
            ),
            daemon=True,
        )
        self.thread.start()
        # serve() asks the gateway which hosts it can lend for once it listens.
        assert self.out.next() == {"t": "lend", "id": 0}
        self.port = int(self.port_file.read_text())

    def reply(self, *frames) -> None:
        for frame in frames:
            self._reply.write(frame if isinstance(frame, str) else json.dumps(frame))
            self._reply.write("\n")
        self._reply.flush()

    def close(self) -> None:
        if not self._reply.closed:
            self._reply.close()
        self.thread.join(WAIT)
        assert not self.thread.is_alive(), "serve() did not return when the gateway closed"


@pytest.fixture
def proxy(tmp_path):
    running = _Proxy(tmp_path)
    try:
        yield running
    finally:
        running.close()


def _call(port, method, path, *, auth=SECRET, host=None, headers=(), body=None, chunks=None):
    """Send one request on a thread; the caller answers it as the gateway."""
    outcome: dict = {}

    def run():
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=WAIT)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host or f"127.0.0.1:{port}")
            if auth is not None:
                conn.putheader("Authorization", f"Bearer {auth}")
            for key, value in headers:
                conn.putheader(key, value)
            if chunks is not None:
                conn.putheader("Transfer-Encoding", "chunked")
                conn.endheaders()
                for piece in chunks:
                    conn.send(b"%x\r\n%s\r\n" % (len(piece), piece))
                conn.send(b"0\r\n\r\n")
            elif body is not None:
                conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(body)
            else:
                conn.endheaders()
            response = conn.getresponse()
            outcome["status"] = response.status
            try:
                outcome["body"] = response.read()
            except http.client.IncompleteRead as cut:
                outcome["cut"] = cut.partial
        except (ConnectionError, http.client.HTTPException) as exc:
            outcome["error"] = exc
        finally:
            conn.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def result() -> dict:
        thread.join(WAIT)
        assert not thread.is_alive(), "the request never finished"
        return outcome

    return result


def test_only_a_caller_with_the_capability_and_exact_host_is_framed(proxy):
    for auth, host in ((None, None), ("cd" * 32, None), (SECRET, "localhost")):
        done = _call(proxy.port, "GET", "/github.com/o/r/info/refs", auth=auth, host=host)
        assert done()["status"] == 403
    assert proxy.out.sent.empty()  # nothing reached the gateway


def test_a_fetch_streams_the_gateways_answer_back_to_git(proxy):
    done = _call(
        proxy.port,
        "GET",
        "/github.com/o/r/info/refs?service=git-upload-pack",
        headers=(("Git-Protocol", "version=2"), ("X-Not-Forwarded", "1")),
    )
    req, end = proxy.out.request()
    assert req["t"] == "req" and req["method"] == "GET"
    assert req["path"] == "/github.com/o/r/info/refs?service=git-upload-pack"
    # Only the headers the gateway may use leave the guest.
    assert req["headers"] == {"git-protocol": "version=2"}
    assert end == {"t": "end", "id": req["id"]}
    rid = req["id"]
    proxy.reply(
        "not json",  # dropped, the stream carries on
        {"t": "body", "id": 999, "b": ""},  # no such request: ignored
        {"t": "resp", "id": rid, "status": 200, "headers": {"Content-Type": "text/plain"}},
        {"t": "body", "id": rid, "b": base64.b64encode(b"hel").decode()},
        {"t": "body", "id": rid, "b": ""},
        {"t": "body", "id": rid, "b": base64.b64encode(b"lo").decode()},
        {"t": "end", "id": rid},
    )
    assert done() == {"status": 200, "body": b"hello"}


@pytest.mark.parametrize("chunked", [True, False], ids=["chunked", "content-length"])
def test_a_push_body_is_framed_and_a_refusal_is_returned(proxy, chunked):
    payload = [b"0032want ", b"abc\n"]
    done = _call(
        proxy.port,
        "POST",
        "/github.com/o/r/git-receive-pack",
        chunks=payload if chunked else None,
        body=None if chunked else b"".join(payload),
    )
    frames = proxy.out.request()
    sent = b"".join(base64.b64decode(f["b"]) for f in frames if f["t"] == "body")
    assert sent == b"".join(payload)
    proxy.reply({"t": "err", "id": frames[0]["id"], "status": 403, "message": "Kiro Crew: denied"})
    assert done() == {"status": 403, "body": b"Kiro Crew: denied\n"}


def test_an_answer_that_is_not_a_response_is_a_502_and_cancels(proxy):
    done = _call(proxy.port, "GET", "/github.com/o/r/info/refs")
    rid = proxy.out.request()[0]["id"]
    proxy.reply({"t": "body", "id": rid, "b": ""})
    assert done()["status"] == 502
    assert proxy.out.next() == {"t": "cancel", "id": rid}


def test_an_error_mid_stream_cuts_the_response_and_cancels(proxy):
    done = _call(proxy.port, "GET", "/github.com/o/r/info/refs")
    rid = proxy.out.request()[0]["id"]
    proxy.reply(
        {"t": "resp", "id": rid, "status": 200, "headers": {}},
        {"t": "body", "id": rid, "b": base64.b64encode(b"part").decode()},
        {"t": "err", "id": rid, "status": 502, "message": "upstream reset"},
    )
    outcome = done()
    assert outcome["status"] == 200 and "body" not in outcome  # git sees a broken response
    assert proxy.out.next() == {"t": "cancel", "id": rid}


def test_no_answer_in_time_is_a_504_and_cancels(proxy, monkeypatch):
    monkeypatch.setattr(git_proxy, "ANSWER_TIMEOUT", 0.05)
    done = _call(proxy.port, "GET", "/github.com/o/r/info/refs")
    rid = proxy.out.request()[0]["id"]
    assert done()["status"] == 504
    assert proxy.out.next() == {"t": "cancel", "id": rid}


def test_past_the_live_request_cap_git_gets_a_503(proxy, monkeypatch):
    monkeypatch.setattr(git_proxy, "MAX_LIVE_REQUESTS", 0)
    assert _call(proxy.port, "GET", "/github.com/o/r/info/refs")()["status"] == 503
    assert proxy.out.sent.empty()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest proxy")
def test_lend_frames_are_kept_beside_the_port_file_and_cleared_on_exit(proxy):
    assert not proxy.lend.exists()  # nothing routed until the gateway answers
    proxy.reply({"t": "lend", "id": 0, "hosts": ["github.com", "Not A Host", 7], "partial": True})
    wait_until(proxy.lend.exists, timeout=WAIT)
    assert proxy.lend.read_text() == f"{proxy.port} * github.com\n"
    proxy.reply({"t": "lend", "id": 0, "hosts": ["dev.azure.com", "github.com"]})
    wait_until(lambda: "*" not in proxy.lend.read_text(), timeout=WAIT)
    assert proxy.lend.read_text() == f"{proxy.port} dev.azure.com github.com\n"
    proxy.close()
    assert proxy.result == [0]
    assert not proxy.lend.exists() and not proxy.port_file.exists()


def test_exit_leaves_another_proxys_files_in_place(proxy):
    # A newer proxy for the same workspace replaced both files: they stay.
    proxy.port_file.write_text(f"{proxy.port + 1}\n")
    proxy.lend.write_text(f"{proxy.port + 1} github.com\n")
    proxy.close()
    assert proxy.port_file.read_text() == f"{proxy.port + 1}\n"
    assert proxy.lend.read_text() == f"{proxy.port + 1} github.com\n"


def test_after_the_gateway_closes_a_request_is_refused_at_once(tmp_path):
    gateway = git_proxy.Gateway(out=_Frames(), inp=io.StringIO(""))
    gateway.read_forever()
    assert gateway.closed.is_set()
    handler = type("Bound", (git_proxy.Handler,), {"gateway": gateway, "secret": SECRET})
    server = git_proxy.Server(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert _call(server.server_address[1], "GET", "/github.com/o/r")()["status"] == 502
    finally:
        server.shutdown()
        server.server_close()
        thread.join(WAIT)


def test_main_reads_the_capability_from_stdin_and_resolves_the_port_file(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(git_proxy, "serve", lambda **kwargs: seen.update(kwargs) or 0)
    monkeypatch.setattr(git_proxy.sys, "stdin", io.StringIO(SECRET + "\n"))
    build = ["--build", git_proxy.build_digest()]
    assert git_proxy.main(["--port-file", "git-proxy.port", *build]) == 0
    assert seen == {"port_file": os.path.abspath("git-proxy.port"), "secret": SECRET}
    monkeypatch.setattr(git_proxy.sys, "stdin", io.StringIO("not-a-capability\n"))
    with pytest.raises(SystemExit, match="invalid git proxy capability"):
        git_proxy.main(["--port-file", "git-proxy.port", *build])
