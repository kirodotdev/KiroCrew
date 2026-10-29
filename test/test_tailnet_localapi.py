"""The daemon client answers only from root-owned transports and never raises.

Each transport (the LocalAPI over a unix socket, the macOS app's LocalAPI port,
the vetted CLI) is driven with a fake of what it reads, so these run on any
host without Tailscale.
"""

from __future__ import annotations

import http.client
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from kiro_crew.dashboard import tailnet
from kiro_crew.dashboard import tailnet_localapi as la

# -- _root_owned_and_sealed ------------------------------------------------ #


def test_a_missing_path_is_not_trusted(tmp_path: Path) -> None:
    assert la._root_owned_and_sealed(str(tmp_path / "absent.sock")) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership")
def test_a_file_the_gateway_user_owns_is_not_trusted(tmp_path: Path) -> None:
    owned = tmp_path / "tailscaled.sock"
    owned.write_text("")
    assert os.getuid() != 0
    assert la._root_owned_and_sealed(str(owned)) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_symlink_is_not_trusted(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.write_text("")
    link = tmp_path / "link"
    link.symlink_to(target)
    assert la._root_owned_and_sealed(str(link)) is False


def _stat(uid: int, mode: int) -> SimpleNamespace:
    return SimpleNamespace(st_uid=uid, st_mode=mode)


@pytest.mark.parametrize(
    ("dir_mode", "expected"),
    [(0o040755, True), (0o040775, False), (0o040757, False)],
    ids=["sealed", "group-writable-dir", "world-writable-dir"],
)
def test_root_ownership_needs_a_directory_nobody_else_writes(
    monkeypatch: pytest.MonkeyPatch, dir_mode: int, expected: bool
) -> None:
    sock = "/run/tailscale/tailscaled.sock"
    real_lstat, real_stat = os.lstat, os.stat

    def lstat(p: Any, *a: Any, **kw: Any) -> Any:
        return _stat(0, 0o140755) if p == sock else real_lstat(p, *a, **kw)

    def stat_(p: Any, *a: Any, **kw: Any) -> Any:
        return _stat(0, dir_mode) if p == os.path.dirname(sock) else real_stat(p, *a, **kw)

    monkeypatch.setattr(la.os, "lstat", lstat)
    monkeypatch.setattr(la.os, "stat", stat_)
    assert la._root_owned_and_sealed(sock) is expected


# -- _http_get ------------------------------------------------------------- #


class _FakeConn:
    def __init__(self, status: int = 200, body: bytes = b"{}", exc: Exception | None = None):
        self._status, self._body, self._exc = status, body, exc
        self.headers: dict[str, str] = {}
        self.closed = False

    def request(self, _method: str, _path: str, headers: dict[str, str]) -> None:
        if self._exc is not None:
            raise self._exc
        self.headers = headers

    def getresponse(self) -> Any:
        body = self._body
        return SimpleNamespace(status=self._status, read=lambda n: body[:n])

    def close(self) -> None:
        self.closed = True


def test_http_get_parses_json_and_sends_the_localapi_host() -> None:
    conn = _FakeConn(body=b'{"Self": {}}')
    answer = la._http_get(conn, "/localapi/v0/status", {"X": "y"})  # type: ignore[arg-type]
    assert answer is not None and answer.data == {"Self": {}}
    assert conn.headers == {"Host": "local-tailscaled.sock", "X": "y"}
    assert conn.closed


@pytest.mark.parametrize(
    "conn",
    [
        _FakeConn(status=403),
        _FakeConn(body=b"not json"),
        _FakeConn(body=b"x" * (la.MAX_ANSWER_BYTES + 1)),
    ],
    ids=["non-200", "bad-json", "over-cap"],
)
def test_http_get_answers_nothing_for_a_bad_reply(conn: _FakeConn) -> None:
    answer = la._http_get(conn, "/p", {})  # type: ignore[arg-type]
    assert answer is not None and answer.data is None


def test_http_get_returns_none_when_the_transport_fails() -> None:
    conn = _FakeConn(exc=OSError("refused"))
    assert la._http_get(conn, "/p", {}) is None  # type: ignore[arg-type]
    assert conn.closed


# -- unix socket transport ------------------------------------------------- #


@pytest.mark.parametrize(
    ("windows", "macos", "expected"),
    [(True, False, ()), (False, True, la.MACOS_SOCKETS), (False, False, la.LINUX_SOCKETS)],
    ids=["windows", "macos", "linux"],
)
def test_socket_paths_follow_the_platform(
    monkeypatch: pytest.MonkeyPatch, windows: bool, macos: bool, expected: tuple[str, ...]
) -> None:
    monkeypatch.setattr(la, "IS_WINDOWS", windows)
    monkeypatch.setattr(la, "IS_MACOS", macos)
    assert la._unix_socket_paths() == expected


def test_an_untrusted_socket_is_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sock = tmp_path / "tailscaled.sock"
    sock.write_text("")
    monkeypatch.setattr(la, "_unix_socket_paths", lambda: (str(tmp_path / "absent"), str(sock)))
    monkeypatch.setattr(la, "_root_owned_and_sealed", lambda _p: False)
    asked: list[str] = []
    monkeypatch.setattr(la, "_http_get", lambda *a: asked.append("x"))
    assert la._localapi_unix("/p") is None
    assert asked == []


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server's method name
        body = json.dumps({"path": self.path, "host": self.headers.get("Host")}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a: Any) -> None:
        return None

    def address_string(self) -> str:
        return "unix"


@pytest.mark.skipif(not hasattr(socketserver, "UnixStreamServer"), reason="unix sockets")
def test_a_trusted_socket_answers_over_the_localapi(monkeypatch: pytest.MonkeyPatch) -> None:
    # A unix socket path is capped near 100 bytes, so the socket lives under the
    # run's short root; the directory is owned by this block before anything binds.
    with tempfile.TemporaryDirectory(prefix=SHORT_TMP_PREFIX, dir=short_tmp_base()) as short:
        path = os.path.join(short, "d.sock")
        server = socketserver.UnixStreamServer(path, _Handler)  # type: ignore[attr-defined,unused-ignore]
        try:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            monkeypatch.setattr(la, "_unix_socket_paths", lambda: (path,))
            monkeypatch.setattr(la, "_root_owned_and_sealed", lambda _p: True)
            answer = la._localapi_unix("/localapi/v0/status?peers=false")
        finally:
            server.shutdown()
            server.server_close()
    assert answer is not None
    assert answer.method == la.METHOD_LOCALAPI_UNIX
    assert answer.data == {"path": "/localapi/v0/status?peers=false", "host": la._LOCALAPI_HOST}


@pytest.mark.skipif(not hasattr(socketserver, "UnixStreamServer"), reason="unix sockets")
def test_a_dead_socket_connection_closes_and_raises(tmp_path: Path) -> None:
    conn = la._UnixHTTPConnection(str(tmp_path / "nobody.sock"), 0.5)
    with pytest.raises(OSError):
        conn.connect()


# -- macOS app transport --------------------------------------------------- #


def test_macsys_is_not_asked_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(la, "IS_MACOS", False)
    assert la._localapi_macsys("/p") is None


def test_macsys_sends_the_token_as_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(la, "IS_MACOS", True)
    monkeypatch.setattr(la, "_macsys_port_and_token", lambda: (41112, "tok"))
    seen: dict[str, Any] = {}

    def fake_get(conn: http.client.HTTPConnection, path: str, headers: dict[str, str]) -> Any:
        seen.update(port=conn.port, path=path, headers=headers)
        return la.Answer({"ok": True}, la.METHOD_NONE)

    monkeypatch.setattr(la, "_http_get", fake_get)
    answer = la._localapi_macsys("/p")
    assert answer == la.Answer({"ok": True}, la.METHOD_LOCALAPI_MACSYS)
    assert seen["port"] == 41112
    assert seen["headers"] == {"Authorization": "Basic OnRvaw=="}


def test_macsys_without_a_port_answers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(la, "IS_MACOS", True)
    monkeypatch.setattr(la, "_macsys_port_and_token", lambda: None)
    assert la._localapi_macsys("/p") is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_macsys_port_needs_a_root_owned_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(la, "MACOS_MACSYS_DIR", str(tmp_path))
    assert la._macsys_port_and_token() is None
    (tmp_path / "ipnport").symlink_to("41112")
    assert la._macsys_port_and_token() is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize(
    ("port", "token", "sealed", "expected"),
    [
        ("41112", "tok\n", True, (41112, "tok")),
        ("41112", "", True, None),
        ("41112", "tok", False, None),
        ("abc", "tok", True, None),
        ("70000", "tok", True, None),
    ],
    ids=["ok", "empty-token", "proof-not-sealed", "not-a-number", "out-of-range"],
)
def test_macsys_port_and_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    port: str,
    token: str,
    sealed: bool,
    expected: tuple[int, str] | None,
) -> None:
    monkeypatch.setattr(la, "MACOS_MACSYS_DIR", str(tmp_path))
    (tmp_path / "ipnport").symlink_to(port)
    (tmp_path / f"sameuserproof-{port}").write_text(token)
    real_lstat = os.lstat
    link = os.path.join(str(tmp_path), "ipnport")

    def lstat(p: Any, *a: Any, **kw: Any) -> Any:
        found = real_lstat(p, *a, **kw)
        return SimpleNamespace(st_uid=0, st_mode=found.st_mode) if p == link else found

    monkeypatch.setattr(la.os, "lstat", lstat)
    monkeypatch.setattr(la, "_root_owned_and_sealed", lambda _p: sealed)
    assert la._macsys_port_and_token() == expected


# -- CLI transport --------------------------------------------------------- #


def test_cli_candidates_use_a_path_hit_only_inside_a_vetted_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vetted = ("/usr/bin/tailscale", la.MACOS_APP_CLI)
    monkeypatch.setattr(tailnet, "_CLI_CANDIDATE_PATHS", vetted)
    monkeypatch.setattr(la.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(tailnet, "_trusted_candidate", lambda p: p)
    monkeypatch.setattr(tailnet, "_candidate_present", lambda _p: True)

    monkeypatch.setattr(la.shutil, "which", lambda _n: "/usr/bin/tailscale")
    assert la.cli_candidates() == [
        ("/usr/bin/tailscale", la.METHOD_CLI_PATH),
        (la.MACOS_APP_CLI, la.METHOD_CLI_APP_BUNDLE),
    ]

    monkeypatch.setattr(la.shutil, "which", lambda _n: "/home/agent/bin/tailscale")
    assert la.cli_candidates() == [
        ("/usr/bin/tailscale", la.METHOD_CLI),
        (la.MACOS_APP_CLI, la.METHOD_CLI_APP_BUNDLE),
    ]


def test_cli_candidates_skip_absent_and_refused_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tailnet, "_CLI_CANDIDATE_PATHS", ("/a/tailscale", "/b/tailscale"))
    monkeypatch.setattr(la.shutil, "which", lambda _n: None)
    monkeypatch.setattr(tailnet, "_candidate_present", lambda p: p == "/b/tailscale")
    monkeypatch.setattr(tailnet, "_trusted_candidate", lambda _p: None)
    assert la.cli_candidates() == []


def _proc(returncode: int = 0, stdout: str = "{}") -> Any:
    return SimpleNamespace(returncode=returncode, stdout=stdout)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (_proc(stdout='{"a": 1}'), ({"a": 1}, False)),
        (_proc(returncode=1), (None, False)),
        (_proc(stdout="x" * (la.MAX_ANSWER_BYTES + 1)), (None, False)),
        (_proc(stdout="nope"), (None, False)),
        (subprocess.TimeoutExpired("tailscale", 3), (None, True)),
        (OSError("no exec"), (None, True)),
    ],
    ids=["json", "nonzero", "over-cap", "bad-json", "timeout", "oserror"],
)
def test_run_cli(monkeypatch: pytest.MonkeyPatch, outcome: Any, expected: Any) -> None:
    def fake_run(*_a: Any, **_kw: Any) -> Any:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(la.subprocess, "run", fake_run)
    assert la._run_cli("/usr/bin/tailscale", ["status", "--json"]) == expected


# -- _ask, whois, status --------------------------------------------------- #


def test_ask_prefers_the_localapi_and_survives_a_failing_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(_p: str) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(la, "_localapi_unix", broken)
    monkeypatch.setattr(la, "_localapi_macsys", lambda _p: la.Answer({"m": 1}, "localapi_macsys"))
    monkeypatch.setattr(la, "cli_candidates", lambda: pytest.fail("CLI asked"))
    assert la._ask("/p", ["status"]) == la.Answer({"m": 1}, "localapi_macsys")


def test_ask_falls_back_to_the_cli_and_reports_a_transient_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(la, "_localapi_unix", lambda _p: None)
    monkeypatch.setattr(la, "_localapi_macsys", lambda _p: None)
    monkeypatch.setattr(
        la, "cli_candidates", lambda: [("/a", la.METHOD_CLI), ("/b", la.METHOD_CLI)]
    )
    answers = {"/a": (None, True), "/b": ({"ok": 1}, False)}
    monkeypatch.setattr(la, "_run_cli", lambda cli, _args: answers[cli])
    assert la._ask("/p", ["status"]) == la.Answer({"ok": 1}, la.METHOD_CLI)

    answers["/b"] = (None, False)
    assert la._ask("/p", ["status"]) == la.Answer(None, la.METHOD_NONE, True)


def test_whois_quotes_the_address_and_status_omits_peers(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        la, "_ask", lambda path, args: asked.append((path, args)) or la.Answer(None, "none")
    )
    la.whois("[fd7a::1]:41641")
    la.status()
    assert asked == [
        (
            "/localapi/v0/whois?addr=%5Bfd7a%3A%3A1%5D%3A41641",
            ["whois", "--json", "[fd7a::1]:41641"],
        ),
        ("/localapi/v0/status?peers=false", ["status", "--json", "--peers=false"]),
    ]
