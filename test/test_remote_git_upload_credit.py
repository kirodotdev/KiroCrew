"""The guest proxy sends a request's body on the gateway's credit.

At most ``UPLOAD_WINDOW`` body frames go out ahead of the credit granted, so the
gateway's frame reader never has to wait on one request; a refusal or a git
client that went away ends the wait.
"""

from __future__ import annotations

import socket
import struct
import sys

import pytest
from test_remote_git_proxy_serve import WAIT, _call, proxy  # noqa: F401 -- the fixture

from kiro_crew.remote_session import git_proxy
from kiro_crew.testing.wait import wait_until

WINDOW = git_proxy.UPLOAD_WINDOW


def _frames_until(proxy, count):  # noqa: F811 -- the fixture's value
    return [proxy.out.next() for _ in range(count)]


def _count_credit_waits(proxy, monkeypatch):  # noqa: F811 -- the fixture's value
    """Count the uploader's credit waits: ``entered`` past ``returned`` means
    a request thread is parked in one, so it cannot be sending."""
    calls = {"entered": 0, "returned": 0}
    real = proxy.gateway.wait_credit

    def counted(rid, alive):
        calls["entered"] += 1
        try:
            real(rid, alive)
        finally:
            calls["returned"] += 1

    monkeypatch.setattr(proxy.gateway, "wait_credit", counted)
    return calls


def _parked_after_window(calls):
    return calls["entered"] == WINDOW + 1 and calls["returned"] == WINDOW


def test_a_large_push_sends_only_its_window_until_the_gateway_grants_more(
    proxy, monkeypatch  # noqa: F811
):
    calls = _count_credit_waits(proxy, monkeypatch)
    extra = 10
    body = b"p" * (git_proxy.CHUNK * (WINDOW + extra))
    done = _call(proxy.port, "POST", "/github.com/o/r/git-receive-pack", body=body)
    req, *pieces = _frames_until(proxy, 1 + WINDOW)
    assert req["t"] == "req" and {f["t"] for f in pieces} == {"body"}
    wait_until(lambda: _parked_after_window(calls), describe=lambda: f"credit waits: {calls}")
    assert proxy.out.sent.empty()  # parked for credit, so nothing past the window
    rid = req["id"]
    proxy.reply(*({"t": "credit", "id": rid} for _ in range(extra)))
    rest = _frames_until(proxy, extra + 1)
    assert [f["t"] for f in rest] == ["body"] * extra + ["end"]
    proxy.reply(
        {"t": "resp", "id": rid, "status": 200, "headers": {}},
        {"t": "end", "id": rid},
    )
    assert done() == {"status": 200, "body": b""}


@pytest.mark.parametrize(
    "answer, expected",
    [
        (
            # A push refused for a protected branch is answered in git's own
            # report-status, as a response, while git is still uploading.
            [
                {"t": "resp", "status": 200, "headers": {"Content-Type": "x-git"}},
                {"t": "body", "b": "cmVmdXNlZA=="},
                {"t": "end"},
            ],
            {"status": 200, "body": b"refused"},
        ),
        (
            [{"t": "err", "status": 403, "message": "Kiro Crew: denied"}],
            {"status": 403, "body": b"Kiro Crew: denied\n"},
        ),
    ],
    ids=["response", "error"],
)
def test_an_answer_while_waiting_for_credit_reaches_git_and_ends_the_body(
    proxy, monkeypatch, answer, expected  # noqa: F811
):
    calls = _count_credit_waits(proxy, monkeypatch)
    body = b"p" * (git_proxy.CHUNK * (WINDOW + 5))
    done = _call(proxy.port, "POST", "/github.com/o/r/git-receive-pack", body=body)
    req, *_ = _frames_until(proxy, 1 + WINDOW)
    wait_until(lambda: _parked_after_window(calls), describe=lambda: f"credit waits: {calls}")
    rid = req["id"]
    proxy.reply(*({**frame, "id": rid} for frame in answer))
    outcome = done()
    assert {k: outcome.get(k) for k in expected} == expected
    # The request has retired: no more body, no end and no cancel went out.
    wait_until(lambda: not proxy.gateway._credits, describe=lambda: "the request never retired")
    assert proxy.out.sent.empty()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest proxy")
def test_git_going_away_while_waiting_for_credit_sends_cancel(proxy, monkeypatch):  # noqa: F811
    monkeypatch.setattr(git_proxy, "CREDIT_POLL_SECS", 0.05)
    size = git_proxy.CHUNK * (WINDOW + 4)
    conn = socket.create_connection(("127.0.0.1", proxy.port), timeout=WAIT)
    try:
        head = (
            "POST /github.com/o/r/git-receive-pack HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{proxy.port}\r\n"
            f"Authorization: Bearer {'ab' * 32}\r\n"
            f"Content-Length: {size}\r\n\r\n"
        )
        conn.sendall(head.encode() + b"p" * (git_proxy.CHUNK * (WINDOW + 1)))
        req, *_ = _frames_until(proxy, 1 + WINDOW)
    finally:
        # A reset, as a killed git leaves it.
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        conn.close()
    assert proxy.out.next() == {"t": "cancel", "id": req["id"]}


def test_a_credit_for_no_live_request_is_ignored(proxy):  # noqa: F811
    proxy.reply({"t": "credit", "id": 999})
    done = _call(proxy.port, "GET", "/github.com/o/r/info/refs")
    req, end = proxy.out.request()
    proxy.reply(
        {"t": "resp", "id": req["id"], "status": 204, "headers": {}},
        {"t": "end", "id": req["id"]},
    )
    assert done()["status"] == 204
    wait_until(lambda: not proxy.gateway._credits, describe=lambda: "credit state left behind")


@pytest.mark.parametrize("closed_first", [True, False])
def test_waiting_for_credit_ends_when_the_link_closes(monkeypatch, closed_first):
    monkeypatch.setattr(git_proxy, "CREDIT_POLL_SECS", 0.01)
    gw = git_proxy.Gateway(out=object(), inp=iter(()))
    rid, _ = gw.open()
    for _ in range(WINDOW):
        gw.wait_credit(rid, lambda: True)
    if closed_first:
        gw.closed.set()
    alive = (lambda: True) if closed_first else (lambda: False)
    with pytest.raises(ConnectionError):
        gw.wait_credit(rid, alive)
    gw.done(rid)
    with pytest.raises(ConnectionError):
        gw.wait_credit(rid, lambda: True)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest proxy")
@pytest.mark.parametrize(
    "build, refusal",
    [
        (None, b"--build"),  # a gateway that predates the pin passes none
        ("0" * 64, b"another Kiro Crew build"),  # a gateway of another build
        ("", None),  # this build: the proxy starts
    ],
    ids=["no-build", "other-build", "same-build"],
)
def test_a_guest_of_another_build_refuses_to_start_instead_of_hanging(tmp_path, build, refusal):
    """Version skew fails closed at start: the guest never frames a request for
    a gateway whose credit protocol may differ, so neither side waits on it."""
    import json
    import subprocess

    port_file = tmp_path / "git-proxy.port"
    argv = [sys.executable, "-u", git_proxy.__file__, "--port-file", str(port_file)]
    if build is not None:
        argv += ["--build", build or git_proxy.build_digest()]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(tmp_path),
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(("ab" * 32 + "\n").encode())
    proc.stdin.flush()
    if refusal is None:
        assert json.loads(proc.stdout.readline()) == {"t": "lend", "id": 0}
        assert port_file.exists()
    # communicate() closes stdin itself, which ends the proxy (or finds it gone).
    _, err = proc.communicate(timeout=WAIT)
    if refusal is None:
        assert proc.returncode == 0
    else:
        assert proc.returncode != 0 and refusal in err
        assert not port_file.exists()
