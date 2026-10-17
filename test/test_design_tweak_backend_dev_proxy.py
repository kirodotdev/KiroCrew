"""Design Tweak backend tests: a project's dev server and its injecting proxy.

The project registry and what removing a project releases, framing only through the
proxy, credential stripping, body caps and the proxied content-type allowlist,
redirects that stay on the proxy, and the cross-platform dev-process lifecycle.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from design_tweak_backend_helpers import isolated_queue_fixture  # noqa: F401
from design_tweak_backend_helpers import _JsonHandler

from kiro_crew.apps.builtins.design_tweak.backend import server
from kiro_crew.platform_compat import IS_POSIX


class TestRemovingAProjectReleasesItsResources:
    """Dropping the registry row is not the same as stopping what it started.

    Each project owns a child dev process, that process's injecting proxy, and
    its own static listener — and the registry row is the only handle to them.
    Removing the row without stopping them orphans a process, a thread and a
    bound port per remove, which accumulates on a long-lived gateway.
    """

    def _handler(self, pid):
        h = server.Handler.__new__(server.Handler)
        h.path = "/projects/remove"
        h.sent: list = []
        h._json = lambda code, payload: h.sent.append((code, payload))  # type: ignore[method-assign]
        h._read_body = lambda: {"id": pid}  # type: ignore[method-assign]
        return h

    def test_remove_stops_the_dev_process_and_the_static_listener(
        self, isolated_queue, monkeypatch
    ):
        stopped: list[tuple[str, str]] = []
        monkeypatch.setattr(server, "_stop_dev_proc", lambda p: stopped.append(("dev", p)))
        monkeypatch.setattr(server, "_stop_static_preview", lambda p: stopped.append(("static", p)))
        monkeypatch.setattr(server, "_save_cfg", lambda _cfg: None)
        monkeypatch.setitem(server._CFG, "projects", [{"id": "p-gone", "path": "/tmp/x"}])

        h = self._handler("p-gone")
        h._h_projects_remove()

        assert h.sent and h.sent[0][0] == 200
        assert ("dev", "p-gone") in stopped, "the child dev server was left running"
        assert ("static", "p-gone") in stopped, "the project's listener was left bound"

    def test_teardown_runs_outside_the_registry_lock(self):
        """`_stop_dev_proc` waits on a SIGKILL escalation.

        Holding `_QUEUE_LOCK` across it would stall every other queue and
        registry operation for the length of a process kill.
        """
        import inspect

        src = inspect.getsource(server.Handler._h_projects_remove)
        lock_block = src.index("with _QUEUE_LOCK")
        assert src.index("_stop_dev_proc") > lock_block
        # The teardown lines must be dedented back out of the `with` body.
        for line in src.splitlines():
            if "_stop_dev_proc(" in line or "_stop_static_preview(" in line:
                assert line.startswith(
                    "        _stop"
                ), f"teardown is still inside the lock body: {line!r}"


class TestProjectRegistryIsSerialized:
    """Project mutations are read-modify-write on a ThreadingHTTPServer.

    Without a lock, a concurrent `select` and `remove` interleave: remove filters
    the project out of the registry while select is between its lookup and its
    write, so the removed project stays `activeId` with `_ROOT` still serving it.
    """

    def test_mutating_handlers_hold_the_lock(self):
        import inspect

        for name in (
            "_h_projects_select",
            "_h_projects_remove",
            "_h_projects_add",
            "_h_projects_preview_url",
        ):
            fn = getattr(server.Handler, name, None)
            if fn is None:
                continue
            src = inspect.getsource(fn)
            assert "_QUEUE_LOCK" in src, f"{name} mutates the registry without the lock"

    def test_select_then_remove_leaves_no_active_ghost(self, tmp_path, monkeypatch):
        """Serialized either way, the end state is never 'removed but active'."""
        proj_dir = tmp_path / "site"
        proj_dir.mkdir()
        (proj_dir / "index.html").write_text("<h1>x</h1>")
        cfg = {
            "projects": [{"id": "p1", "path": str(proj_dir), "name": "site"}],
            "activeId": "",
            "counter": 0,
        }
        monkeypatch.setattr(server, "_CFG", cfg)
        monkeypatch.setattr(server, "_save_cfg", lambda _c: None)
        monkeypatch.setattr(server, "_ROOT", "")

        class _H(server.Handler):
            def __init__(self, body):
                self._body = body
                self.sent = []

            def _read_body(self):
                return self._body

            def _json(self, code, payload):
                self.sent.append((code, payload))

        server.Handler._h_projects_select(_H({"id": "p1"}))
        assert server._CFG["activeId"] == "p1"
        server.Handler._h_projects_remove(_H({"id": "p1"}))
        # The invariant: an id that is gone from the registry is never active.
        ids = {p["id"] for p in server._CFG["projects"]}
        assert "p1" not in ids
        assert server._CFG["activeId"] == ""
        assert server._ROOT == ""


class TestProxyFailureNeverFramesTheBareDevServer:
    """If the injecting proxy cannot bind, frame NOTHING — not the dev server.

    The proxy is the barrier that strips `Cookie`/`Authorization` (see
    `TestDevProxyStripsCredentials`). Because cookies are host-scoped but
    PORT-agnostic, `127.0.0.1:<dev-port>` receives the dashboard's own session
    cookie, so falling back to the bare dev URL would hand the previewed
    project's code the very credential the proxy exists to withhold. Losing the
    overlay is a feature regression; leaking the session cookie is a security
    one, so the preview reports itself unreachable instead.
    """

    def test_bind_failure_yields_empty_not_the_dev_url(self, monkeypatch):
        monkeypatch.setattr(server, "_DEV_PROCS", {})
        monkeypatch.setattr(server, "_start_inject_proxy", lambda _u: (None, ""))

        assert server._front_with_proxy("p1", "http://127.0.0.1:5173") == ""

    def test_a_live_proxy_is_still_returned(self, monkeypatch):
        monkeypatch.setattr(server, "_DEV_PROCS", {})
        monkeypatch.setattr(
            server, "_start_inject_proxy", lambda _u: (object(), "http://127.0.0.1:49999")
        )

        assert server._front_with_proxy("p1", "http://127.0.0.1:5173") == ("http://127.0.0.1:49999")

    def test_no_caller_reports_injected_on_an_empty_url(self):
        """`injected` was `framed != dev_url`, which is TRUE for the empty string.

        Asserted against the two real sites by name — a source scan that matched
        nothing would pass vacuously.
        """
        import inspect

        sites = [server._start_dev_proc, server.Handler._h_dev_server_start]
        checked = 0
        for fn in sites:
            for line in inspect.getsource(fn).splitlines():
                stripped = line.strip()
                # Only the dict ENTRY, not the comment that explains it.
                if not stripped.startswith('"injected":'):
                    continue
                checked += 1
                assert (
                    "bool(framed)" in stripped
                ), f"{fn.__name__} reports injected on an unbindable proxy: {stripped}"
        assert checked == 2, f"expected 2 injected sites, inspected {checked}"


class TestDevProxyStripsCredentials:
    """The dev proxy must not relay the browser's dashboard credentials upstream.

    The proxy advertises itself as ``http://127.0.0.1:<ephemeral port>``. Cookies
    are scoped by HOST and ignore the port, so when the dashboard is also served
    from ``127.0.0.1`` the browser attaches its ``SameSite=Lax`` auth cookie to
    every proxied request. Forwarding that verbatim would hand a usable session
    token to an arbitrary ``npm run dev`` process belonging to the project.
    """

    def test_credential_headers_are_declared(self):
        assert "cookie" in server._CREDENTIAL_REQUEST_HEADERS
        assert "authorization" in server._CREDENTIAL_REQUEST_HEADERS

    def test_response_side_blocks_set_cookie(self):
        """The dev server must not be able to set cookies on OUR response.

        Cookies ignore ports and this proxy shares 127.0.0.1 with the dashboard,
        so an upstream ``Set-Cookie`` naming the gateway's cookie would replace
        the dashboard session in the browser.
        """
        assert "set-cookie" in server._CREDENTIAL_RESPONSE_HEADERS
        assert "set-cookie2" in server._CREDENTIAL_RESPONSE_HEADERS

    def test_relay_drops_upstream_set_cookie(self):
        """Reproduce the response-header filter over a realistic header set."""
        upstream = [
            ("Content-Type", "text/html"),
            ("Set-Cookie", "kirocrew_session=attacker; Path=/"),
            ("set-cookie", "another=1"),
            ("Cache-Control", "no-store"),
            ("Connection", "keep-alive"),
        ]
        forwarded = []
        for key, value in upstream:
            low = key.lower()
            if low in server._HOP_BY_HOP or low == "content-length":
                continue
            if low in server._CREDENTIAL_RESPONSE_HEADERS:
                continue
            if not server._HEADER_NAME_RE.match(key):
                continue
            forwarded.append((key, value))

        names = {k.lower() for k, _ in forwarded}
        assert "set-cookie" not in names
        assert not any("attacker" in v for _, v in forwarded)
        # Benign headers still pass -- this is a targeted strip, not a blanket one.
        assert ("Content-Type", "text/html") in forwarded
        assert ("Cache-Control", "no-store") in forwarded

    def test_websocket_handshake_strips_set_cookie(self):
        """The 101 must be sanitized too, not pumped through raw.

        `_relay_http` filtered credential response headers, but the WebSocket
        path replayed the handshake and then started a byte pump -- so an
        upstream `Set-Cookie` on the 101 bypassed the filter entirely and could
        replace the dashboard's host-scoped session cookie (cookies ignore
        ports, and this proxy shares 127.0.0.1 with the dashboard).
        """
        raw_head = (
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n"
            b"Set-Cookie: kirocrew_session=attacker; Path=/\r\n"
            b"set-cookie: another=1\r\n"
            b"Sec-WebSocket-Protocol: vite-hmr\r\n"
        )
        head_lines = raw_head.rstrip(b"\r\n").split(b"\r\n")
        sanitized = [head_lines[0]]
        for header_line in head_lines[1:]:
            name, _, _value = header_line.partition(b":")
            if name.strip().lower().decode("latin-1", "replace") in (
                server._CREDENTIAL_RESPONSE_HEADERS
            ):
                continue
            sanitized.append(header_line)
        out = b"\r\n".join(sanitized)

        assert b"Set-Cookie" not in out and b"set-cookie" not in out
        assert b"attacker" not in out
        # The status line and the handshake headers HMR needs must survive.
        assert out.startswith(b"HTTP/1.1 101 Switching Protocols")
        assert b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in out
        assert b"Sec-WebSocket-Protocol: vite-hmr" in out

    def test_relay_ws_sanitizes_before_pumping(self):
        """Structural: the pump must not be reachable before the strip.

        Guards the ordering that made this a bug -- `dst_sock.sendall` (the pump)
        must come AFTER `_CREDENTIAL_RESPONSE_HEADERS` is consulted in the source
        of `_relay_ws`, otherwise the 101 escapes unfiltered again.
        """
        import inspect

        src = inspect.getsource(server._DevProxyHandler._relay_ws)
        assert "_CREDENTIAL_RESPONSE_HEADERS" in src, "the 101 is no longer sanitized"
        assert src.index("_CREDENTIAL_RESPONSE_HEADERS") < src.index(
            "dst_sock.sendall"
        ), "the byte pump runs before the credential strip"

    def test_credential_headers_are_not_hop_by_hop(self):
        """They need their own set: hop-by-hop stripping would not cover them."""
        assert not (server._CREDENTIAL_REQUEST_HEADERS & server._HOP_BY_HOP)

    @pytest.mark.parametrize("header", ["Cookie", "cookie", "Authorization"])
    def test_relay_http_drops_credentials(self, header):
        """Reproduce _relay_http's header filter over a realistic header set."""
        incoming = {
            header: "kirocrew_token=super-secret",
            "Accept": "text/html",
            "Accept-Encoding": "gzip",
            "Host": "127.0.0.1:5476",
            "Connection": "keep-alive",
            "User-Agent": "probe",
        }
        forwarded = {}
        for key, value in incoming.items():
            low = key.lower()
            if low in server._HOP_BY_HOP or low in ("accept-encoding", "host"):
                continue
            if low in server._CREDENTIAL_REQUEST_HEADERS:
                continue
            forwarded[key] = value

        assert header not in forwarded
        assert not any("secret" in v for v in forwarded.values())
        # The benign headers still get through — this is a targeted strip, not a
        # blanket one.
        assert forwarded["Accept"] == "text/html"
        assert forwarded["User-Agent"] == "probe"

    def test_websocket_handshake_replay_drops_credentials(self):
        """The HMR upgrade path replays headers verbatim, so it needs the same strip."""
        incoming = {
            "Host": "127.0.0.1:5476",
            "Cookie": "kirocrew_token=super-secret",
            "Upgrade": "websocket",
            "Sec-WebSocket-Key": "abc",
        }
        lines = []
        for key, value in incoming.items():
            if key.lower() in server._CREDENTIAL_REQUEST_HEADERS:
                continue
            lines.append(f"{key}: {value}")

        replay = "\r\n".join(lines)
        assert "super-secret" not in replay
        # The upgrade must survive, or hot reload breaks.
        assert "Upgrade: websocket" in replay
        assert "Sec-WebSocket-Key: abc" in replay


@pytest.fixture()
def fake_proxy(monkeypatch):
    """Stub `_start_inject_proxy`, recording every dev URL it is asked to front."""
    calls: list[str] = []

    class _Srv:
        def shutdown(self):
            pass

        def server_close(self):
            pass

    def fake_start(dev_url):
        calls.append(dev_url)
        return _Srv(), f"http://127.0.0.1:4{len(calls):04d}/"

    monkeypatch.setattr(server, "_start_inject_proxy", fake_start)
    monkeypatch.setattr(server, "_DEV_PROCS", {})
    return calls


class TestPersistedDevUrlIsFrontedWithProxy:
    """A persisted dev URL must be framed through the INJECTING proxy.

    Returned bare it renders perfectly — which is why this was invisible — but
    the proxy is the only thing that injects the select-to-edit overlay, so the
    whole feature silently did nothing on exactly the framework projects that
    need a dev server.
    """

    def _list(self, tmp_path, monkeypatch, preview_url):
        proj = {"id": "p1", "path": str(tmp_path), "name": "proj", "previewUrl": preview_url}
        monkeypatch.setitem(server._CFG, "projects", [proj])
        monkeypatch.setitem(server._CFG, "activeId", "p1")
        h = _JsonHandler()
        h._h_projects_list()
        code, payload = h.json_sent[0]
        assert code == 200
        return payload["projects"][0]

    def test_persisted_url_is_replaced_by_the_proxy_url(self, tmp_path, monkeypatch, fake_proxy):
        row = self._list(tmp_path, monkeypatch, "http://127.0.0.1:5173")
        assert fake_proxy == ["http://127.0.0.1:5173"]
        assert row["previewUrl"] == "http://127.0.0.1:40001/"
        # The bare dev server is still reported, for the UI's "Dev server" label.
        assert row["devUrl"] == "http://127.0.0.1:5173"
        assert row.get("previewMode") != "static"

    def test_a_rejected_nonempty_url_is_cleared_not_framed_bare(self, tmp_path, monkeypatch):
        """A persisted value that fails the loopback allow-list must not be framed.

        It matches neither the live-proxy branch nor the static branch (which only
        fires on an EMPTY value), so before the fix it fell through and was handed
        back verbatim — framed directly, bypassing the proxy that strips
        `Cookie`/`Authorization`, sending the dashboard's host-scoped session
        cookie to whatever it named.
        """
        for bad in (
            "https://127.0.0.1:5173",  # allow-list is http-only
            "http://evil.example.com",  # not loopback
            "http://127.0.0.1:99999",  # port out of range
            "file:///etc/passwd",
        ):
            row = self._list(tmp_path, monkeypatch, bad)
            assert row["previewUrl"] == "", f"{bad} was handed back to the iframe"

    def test_localhost_form_is_fronted_too(self, tmp_path, monkeypatch, fake_proxy):
        row = self._list(tmp_path, monkeypatch, "http://localhost:3000")
        assert fake_proxy == ["http://localhost:3000"]
        assert row["previewUrl"].startswith("http://127.0.0.1:4")

    def test_polling_reuses_one_proxy(self, tmp_path, monkeypatch, fake_proxy):
        """The panel polls this endpoint — it must not leak a listener per call."""
        first = self._list(tmp_path, monkeypatch, "http://127.0.0.1:5173")
        for _ in range(4):
            again = self._list(tmp_path, monkeypatch, "http://127.0.0.1:5173")
            assert again["previewUrl"] == first["previewUrl"]
        assert fake_proxy == ["http://127.0.0.1:5173"], "started a second proxy"

    def test_proxy_failure_leaves_the_preview_unreachable(self, tmp_path, monkeypatch):
        """No preview beats a leaking one.

        Framing the bare dev server is not an acceptable degradation, even
        "without select-to-edit": the
        proxy is also what strips the dashboard's `Cookie` header, and cookies
        ignore the port, so the bare dev server on the same host receives the
        session cookie. An empty `previewUrl` renders the unreachable state.
        """
        monkeypatch.setattr(server, "_DEV_PROCS", {})
        monkeypatch.setattr(server, "_start_inject_proxy", lambda dev_url: (None, ""))
        row = self._list(tmp_path, monkeypatch, "http://127.0.0.1:5173")
        assert row["previewUrl"] == ""
        assert row.get("devUrl") == "http://127.0.0.1:5173"

    @pytest.mark.parametrize(
        "bad",
        [
            "http://169.254.169.254:80",  # cloud metadata
            "http://evil.example.com:5173",
            "https://127.0.0.1:5173",  # non-http scheme
        ],
    )
    def test_non_loopback_persisted_url_is_never_fronted(
        self, tmp_path, monkeypatch, fake_proxy, bad
    ):
        """The allow-list is re-asserted at the sink: this value is read off disk
        and would become a proxy UPSTREAM.

        It is CLEARED, not handed back. Handing it back — "left exactly as
        today (framed bare)" — is the credential leak: framing it directly
        bypasses the proxy that strips `Cookie`/`Authorization`, and cookies are
        host-scoped but port-agnostic, so the dashboard's own session cookie
        reached whatever the value named.
        """
        row = self._list(tmp_path, monkeypatch, bad)
        assert fake_proxy == [], f"started a proxy to {bad}"
        assert row["previewUrl"] == "", f"{bad} was handed back to the iframe"

    def test_live_proxy_still_wins(self, tmp_path, monkeypatch, fake_proxy):
        """Precedence is unchanged: a running dev server we started/adopted is
        the truth, a persisted URL is only the fallback."""
        server._DEV_PROCS["p1"] = {
            "proc": None,
            "pgid": None,
            "url": "http://127.0.0.1:5999",
            "proxy": object(),
            "proxyUrl": "http://127.0.0.1:41234/",
            "proxyFor": "http://127.0.0.1:5999",
        }
        row = self._list(tmp_path, monkeypatch, "http://127.0.0.1:5173")
        assert row["previewUrl"] == "http://127.0.0.1:41234/"
        assert row["devUrl"] == "http://127.0.0.1:5999"
        assert fake_proxy == []

    def test_no_persisted_url_still_gets_the_static_preview(self, tmp_path, monkeypatch):
        """The static-from-disk path is untouched by this change."""
        monkeypatch.setattr(server, "_DEV_PROCS", {})
        monkeypatch.setattr(
            server, "_static_preview_url", lambda pid: f"http://127.0.0.1:7777/{pid}/"
        )
        row = self._list(tmp_path, monkeypatch, "")
        assert row["previewUrl"] == "http://127.0.0.1:7777/p1/"
        assert row["previewMode"] == "static"


class TestFrontWithProxyIdempotence:
    """`_front_with_proxy` is now called per poll, so it must be reuse-first."""

    def test_second_call_for_the_same_url_reuses(self, fake_proxy):
        first = server._front_with_proxy("p1", "http://127.0.0.1:5173")
        second = server._front_with_proxy("p1", "http://127.0.0.1:5173")
        assert first == second
        assert len(fake_proxy) == 1

    def test_a_changed_dev_url_rebuilds(self, fake_proxy):
        first = server._front_with_proxy("p1", "http://127.0.0.1:5173")
        second = server._front_with_proxy("p1", "http://127.0.0.1:3000")
        assert first != second
        assert fake_proxy == ["http://127.0.0.1:5173", "http://127.0.0.1:3000"]
        assert server._DEV_PROCS["p1"]["proxyFor"] == "http://127.0.0.1:3000"

    def test_a_stopped_proxy_is_not_reused(self, fake_proxy):
        server._front_with_proxy("p1", "http://127.0.0.1:5173")
        server._stop_inject_proxy(server._DEV_PROCS["p1"])
        # A dead listener's URL must never be handed to the iframe.
        assert server._DEV_PROCS["p1"]["proxyUrl"] == ""
        server._front_with_proxy("p1", "http://127.0.0.1:5173")
        assert len(fake_proxy) == 2

    def test_records_the_dev_url_for_an_adopted_server(self, fake_proxy):
        server._front_with_proxy("p1", "http://127.0.0.1:5173")
        rec = server._DEV_PROCS["p1"]
        assert rec["url"] == "http://127.0.0.1:5173"
        assert rec["adopted"] is True


class TestPersistedMalformedPortDoesNotBreakProjects:
    """A malformed persisted dev URL must not take `/projects` down.

    This is the regression the port validation exists for: pre-fix,
    `_valid_target` waved `http://localhost:notaport` through, it was persisted
    onto the project, and the next `/projects` poll raised ValueError while
    reading `.port` — so EVERY subsequent project-list request 500'd. A
    self-inflicted, persistent outage from one typed character.
    """

    def _list(self, tmp_path, monkeypatch, preview_url):
        proj = {"id": "p1", "path": str(tmp_path), "name": "proj", "previewUrl": preview_url}
        monkeypatch.setitem(server._CFG, "projects", [proj])
        monkeypatch.setitem(server._CFG, "activeId", "p1")
        h = _JsonHandler()
        h._h_projects_list()
        return h.json_sent[0]

    @pytest.mark.parametrize(
        "bad", ["http://localhost:notaport", "http://localhost:99999", "http://127.0.0.1:0"]
    )
    def test_projects_still_answers_200(self, tmp_path, monkeypatch, fake_proxy, bad):
        code, payload = self._list(tmp_path, monkeypatch, bad)
        assert code == 200, payload
        assert fake_proxy == [], f"started a proxy to {bad}"
        # Cleared, same as any other non-allow-listed URL: handing it back would
        # frame it bare, past the proxy that strips the dashboard session cookie.
        # The point of this test is that the endpoint still answers 200 rather
        # than 500 — that invariant is unchanged.
        assert payload["projects"][0]["previewUrl"] == ""

    @pytest.mark.parametrize("bad", ["http://localhost:notaport", "http://localhost:99999"])
    def test_malformed_port_is_refused_at_the_write_paths(self, monkeypatch, bad):
        """It should never reach disk in the first place."""
        monkeypatch.setitem(server._CFG, "projects", [{"id": "p1", "path": "/tmp", "name": "proj"}])
        monkeypatch.setattr(server, "_save_cfg", lambda cfg: None)
        h = _JsonHandler(body=json.dumps({"id": "p1", "previewUrl": bad}).encode("utf-8"))
        h._h_projects_preview_url()
        assert h.json_sent[0][0] == 400
        assert "previewUrl" not in server._CFG["projects"][0]


class _RelayProbe(server._DevProxyHandler):
    """A `_DevProxyHandler` with the socket plumbing replaced by recorders.

    Instantiated via `__new__` so `BaseHTTPRequestHandler.__init__` (which wants a
    real connection and immediately parses a request) never runs — the point is to
    drive `_relay_http` alone.
    """

    def __init__(self, *, command="GET", path="/", headers=None, body=b""):
        self.command = command
        self.path = path
        self.headers = headers or {}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.upstream_host = "127.0.0.1"
        self.upstream_port = 5173
        self.close_connection = False
        self.errors: list[tuple[int, str]] = []
        self.status: int | None = None
        self.sent_headers: list[tuple[str, str]] = []

    def send_error(self, code, message=None, explain=None):  # noqa: D102
        self.errors.append((code, message or ""))

    def send_response(self, code, message=None):  # noqa: D102
        self.status = code

    def send_header(self, key, value):  # noqa: D102
        self.sent_headers.append((key, value))

    def end_headers(self):  # noqa: D102
        pass

    def log_message(self, fmt, *args):  # noqa: D102
        pass


class _FakeUpstreamResponse:
    def __init__(self, payload: bytes, ctype: str = "application/octet-stream"):
        self._buf = io.BytesIO(payload)
        self.status = 200
        self._ctype = ctype

    def read(self, amt=None):
        return self._buf.read(amt) if amt is not None else self._buf.read()

    def getheader(self, name, default=None):
        return self._ctype if name.lower() == "content-type" else default

    def getheaders(self):
        return [("Content-Type", self._ctype)]


class TestDevProxyBodyCaps:
    """The dev proxy buffers both directions whole, so both need a ceiling.

    Without one, a single oversized preview request or dev-server response is
    enough to OOM the backend — the proxy reads it all into one `bytes`.
    """

    def _instance(self, **kw):
        probe = _RelayProbe.__new__(_RelayProbe)
        _RelayProbe.__init__(probe, **kw)
        return probe

    def test_oversized_request_body_rejected(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            server.http.client,
            "HTTPConnection",
            lambda *a, **k: called.append(a) or pytest.fail("upstream was contacted"),
        )
        over = server.MAX_BODY_BYTES + 1
        probe = self._instance(
            command="POST", headers={"Content-Length": str(over)}, body=b"x" * 16
        )
        probe._relay_http()

        assert probe.errors and probe.errors[0][0] == 413
        # The unread body would desync the next request on a kept-alive socket.
        assert probe.close_connection is True
        assert not called

    def test_incomplete_request_body_rejected(self, monkeypatch):
        """A permitted Content-Length the client never delivers must not relay.

        The size cap only screens the DECLARED length, so a client can promise
        an allowed size and send nothing. Left unchecked the short read was
        swallowed (`body = b""`) and a truncated request went upstream; the same
        promise on a real socket parks the handler thread instead, which is why
        the handlers now carry `_CLIENT_READ_TIMEOUT`.
        """
        called = []
        monkeypatch.setattr(
            server.http.client,
            "HTTPConnection",
            lambda *a, **k: called.append(a) or pytest.fail("upstream was contacted"),
        )
        probe = self._instance(
            command="POST", headers={"Content-Length": "4096"}, body=b"only-a-few"
        )
        probe._relay_http()

        assert probe.errors and probe.errors[0][0] == 400
        assert probe.close_connection is True
        assert not called, "a truncated body must never reach the dev server"

    def test_request_body_at_cap_is_relayed(self, monkeypatch):
        seen: dict[str, Any] = {}

        class _Conn:
            def __init__(self, *a, **k):
                pass

            def request(self, method, path, body=None, headers=None):
                seen["body"] = body

            def getresponse(self):
                return _FakeUpstreamResponse(b"ok")

            def close(self):
                pass

        monkeypatch.setattr(server.http.client, "HTTPConnection", _Conn)
        payload = b"y" * server.MAX_BODY_BYTES
        probe = self._instance(
            command="POST", headers={"Content-Length": str(len(payload))}, body=payload
        )
        probe._relay_http()

        assert not probe.errors, "the cap must be inclusive, not off by one"
        assert seen["body"] == payload

    def test_oversized_upstream_response_rejected(self, monkeypatch):
        """A huge dev-server response must not be buffered in full."""
        requested: list[int | None] = []
        over = server.MAX_STATIC_BYTES + 1

        class _Conn:
            def __init__(self, *a, **k):
                pass

            def request(self, *a, **k):
                pass

            def getresponse(self):
                outer = self

                class _Resp(_FakeUpstreamResponse):
                    def read(self, amt=None):
                        requested.append(amt)
                        # Pretend the upstream has `over` bytes available: return
                        # exactly what was asked for, never more.
                        return b"z" * min(amt, over) if amt is not None else b"z" * over

                del outer
                return _Resp(b"")

            def close(self):
                pass

        monkeypatch.setattr(server.http.client, "HTTPConnection", _Conn)
        probe = self._instance()
        probe._relay_http()

        assert probe.errors and probe.errors[0][0] == 502
        assert probe.status is None, "an oversized body must not start a 200 response"
        # Bounded read: never a bare `read()`, and never more than cap + 1 byte.
        assert requested and all(a is not None for a in requested)
        assert max(a for a in requested if a is not None) == server.MAX_STATIC_BYTES + 1

    def test_normal_response_still_relayed(self, monkeypatch):
        class _Conn:
            def __init__(self, *a, **k):
                pass

            def request(self, *a, **k):
                pass

            def getresponse(self):
                return _FakeUpstreamResponse(b"<html><body>hi</body></html>", "text/html")

            def close(self):
                pass

        monkeypatch.setattr(server.http.client, "HTTPConnection", _Conn)
        probe = self._instance()
        probe._relay_http()

        assert not probe.errors
        assert probe.status == 200
        out = probe.wfile.getvalue()
        assert b"hi" in out
        # HTML still gets the overlay injected — the cap did not break the rewrite.
        assert server._OVERLAY_PATH.encode() in out


class TestDevProxyContentTypeIsAllowlisted:
    """A proxied reply carries one of OUR literals, never the upstream's value.

    `_PROXY_CTYPES` and `_safe_upstream_ctype` exist because the dev server is the
    project's own process but still an unaudited one whose headers land in our
    response. The selector was written, tested in isolation, and never called:
    `_relay_http` forwarded every upstream header through `_header_value` alone, so
    the media type and its charset reached the browser as sent. `_header_value`
    still stopped response splitting, which is why this survived -- what was lost
    is the mapping to a closed set.
    """

    def _relay(self, monkeypatch, upstream_ctype, path="/"):
        class _Conn:
            def __init__(self, *a, **k):
                pass

            def request(self, *a, **k):
                pass

            def getresponse(self):
                return _FakeUpstreamResponse(b"body", upstream_ctype)

            def close(self):
                pass

        monkeypatch.setattr(server.http.client, "HTTPConnection", _Conn)
        probe = _RelayProbe.__new__(_RelayProbe)
        _RelayProbe.__init__(probe, path=path)
        probe._relay_http()
        assert not probe.errors
        return {k.lower(): v for k, v in probe.sent_headers}

    def test_upstream_charset_is_normalised(self, monkeypatch):
        """The one case a verbatim forward actually changed browser behaviour."""
        sent = self._relay(monkeypatch, "text/html; charset=iso-8859-1")
        assert sent["content-type"] == "text/html; charset=utf-8"

    def test_unrecognised_media_type_falls_back_to_the_request_path(self, monkeypatch):
        sent = self._relay(monkeypatch, "bogus/thing", path="/app.css")
        assert sent["content-type"] == _safe_ctype_for_css()

    def test_a_header_smuggled_into_the_media_type_cannot_survive(self, monkeypatch):
        sent = self._relay(monkeypatch, "evil/x\r\nSet-Cookie: a=b", path="/x.css")
        assert sent["content-type"] == _safe_ctype_for_css()
        assert "set-cookie" not in sent


def _safe_ctype_for_css() -> str:
    """The literal the selector maps an unknown media type on a `.css` path to."""
    return server._safe_upstream_ctype("bogus/thing", "/x.css")


class TestDevProcCrossPlatform:
    """Starting and stopping a project's dev server must not use POSIX-only calls.

    `os.getpgid` / `os.killpg` do not exist on Windows and `start_new_session=True`
    raises there, so the spawn path has to go through `platform_compat`.
    """

    def test_no_direct_posix_process_calls(self):
        src = Path(server.__file__).read_text("utf-8")
        assert "os.killpg(" not in src, "use platform_compat.kill_process_tree"
        assert "start_new_session=True" not in src, "start_new_session is POSIX-only"
        assert "creationflags=CREATE_NEW_PROCESS_GROUP" in src

    def test_stop_uses_kill_process_tree(self, monkeypatch):
        killed: list[tuple[int, int]] = []

        class _Proc:
            pid = 4321
            waits = 0

            def wait(self, timeout=None):
                type(self).waits += 1
                return 0

        monkeypatch.setattr(
            server, "kill_process_tree", lambda pid, sig: killed.append((pid, sig)) or True
        )
        monkeypatch.setattr(server, "_stop_inject_proxy", lambda rec: None)
        server._DEV_PROCS["p-stop"] = {"proc": _Proc(), "pgid": 4321}
        try:
            assert server._stop_dev_proc("p-stop") is True
        finally:
            server._DEV_PROCS.pop("p-stop", None)

        assert killed == [(4321, server.SIGTERM)]

    def test_stop_escalates_when_sigterm_ignored(self, monkeypatch):
        killed: list[tuple[int, int]] = []

        class _Stubborn:
            pid = 999

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("dev", timeout or 0)

        monkeypatch.setattr(
            server, "kill_process_tree", lambda pid, sig: killed.append((pid, sig)) or True
        )
        monkeypatch.setattr(server, "_stop_inject_proxy", lambda rec: None)
        server._DEV_PROCS["p-hard"] = {"proc": _Stubborn(), "pgid": 999}
        try:
            server._stop_dev_proc("p-hard")
        finally:
            server._DEV_PROCS.pop("p-hard", None)

        assert killed == [(999, server.SIGTERM), (999, server.SIGKILL)]

    def test_in_proc_tree_walks_parents_without_pgid(self, monkeypatch):
        """The Windows path (pgid=None) matches a grandchild via the parent chain."""
        chain = {30: 20, 20: 10, 10: 0}
        monkeypatch.setattr(server, "get_ppid", lambda pid: chain.get(pid, 0))

        assert server._in_proc_tree(30, 10, None) is True
        assert server._in_proc_tree(30, 99, None) is False

    def test_in_proc_tree_terminates_on_cyclic_parent_map(self, monkeypatch):
        monkeypatch.setattr(server, "get_ppid", lambda pid: {7: 8, 8: 7}.get(pid, 0))
        assert server._in_proc_tree(7, 999, None) is False

    @pytest.mark.skipif(not IS_POSIX, reason="os.getpgid is POSIX-only")
    def test_in_proc_tree_uses_group_on_posix(self, monkeypatch):
        monkeypatch.setattr(server.os, "getpgid", lambda pid: 55 if pid == 5 else 66)
        assert server._in_proc_tree(5, 1, 55) is True
        assert server._in_proc_tree(6, 1, 55) is False


class TestRedirectStaysOnTheProxy:
    """An upstream redirect must not walk the iframe off this proxy.

    The proxy shares `127.0.0.1` with the dashboard and cookies are host-scoped
    but PORT-agnostic. A `Location` naming the dev server's own port would make
    the browser navigate straight to the project's process — a hop that never
    passes through this handler, so the request-side credential strip cannot see
    it — and send the dashboard's session cookie along with it.
    """

    def _probe(self, upstream_port=5173, proxy_port=45678):
        probe = _RelayProbe.__new__(_RelayProbe)
        _RelayProbe.__init__(probe)
        probe.upstream_port = upstream_port
        probe.proxy_port = proxy_port
        return probe

    def test_absolute_redirect_to_upstream_is_repointed_at_the_proxy(self):
        got = self._probe()._keep_redirect_local("http://127.0.0.1:5173/dashboard?a=1#frag")
        assert got == "http://127.0.0.1:45678/dashboard?a=1#frag"

    def test_localhost_spelling_is_also_caught(self):
        """`localhost` and `127.0.0.1` are the same upstream; both must rewrite."""
        got = self._probe()._keep_redirect_local("http://localhost:5173/x")
        assert got == "http://127.0.0.1:45678/x"

    def test_relative_redirect_is_untouched(self):
        """It already resolves against our origin — rewriting would be noise."""
        p = self._probe()
        assert p._keep_redirect_local("/next") == "/next"
        assert p._keep_redirect_local("next?a=1") == "next?a=1"

    def test_offhost_redirect_is_untouched(self):
        """The dashboard cookie is scoped to this host, so it cannot ride along."""
        p = self._probe()
        for url in (
            "https://cdn.example.com/asset.js",
            "http://192.168.1.5:5173/x",
        ):
            assert p._keep_redirect_local(url) == url

    def test_loopback_on_a_different_port_is_untouched(self):
        """Only the UPSTREAM's port is ours to reclaim.

        Rewriting every loopback port would hijack a redirect to an unrelated
        local service — including the dashboard itself.
        """
        p = self._probe()
        assert p._keep_redirect_local("http://127.0.0.1:5476/chat") == "http://127.0.0.1:5476/chat"

    def test_implicit_port_80_matches_an_upstream_on_80(self):
        p = self._probe(upstream_port=80)
        assert p._keep_redirect_local("http://127.0.0.1/x") == "http://127.0.0.1:45678/x"

    def test_non_http_scheme_is_untouched(self):
        """A `javascript:`/`data:` Location is not ours to rewrite into an http URL."""
        p = self._probe()
        for url in ("javascript:alert(1)", "data:text/html,<b>x</b>"):
            assert p._keep_redirect_local(url) == url

    def test_malformed_port_is_forwarded_not_raised(self):
        """A non-numeric authority must not take the relay thread down.

        `urlparse` accepts `http://localhost:notaport/` without complaint —
        `.port` is a LAZY property, so the ValueError surfaces at the first
        reader, which is this helper and not the `urlparse` call. Left unguarded
        it escapes the handler, `ThreadingHTTPServer` closes the connection with
        no HTTP response at all, and the preview dies on a stderr traceback.
        Forward the header untouched instead: an authority we cannot parse is not
        one we can prove is the upstream.
        """
        p = self._probe()
        for url in (
            "http://localhost:notaport/x",
            "http://127.0.0.1:99999/x",  # out of range — `.port` rejects it too
            "http://localhost:-1/x",
        ):
            assert p._keep_redirect_local(url) == url

    def test_relay_rewrites_location_end_to_end(self, monkeypatch):
        """Prove it through `_relay_http`, not just the helper in isolation."""

        class _Conn:
            def __init__(self, *a, **k):
                pass

            def request(self, *a, **k):
                pass

            def getresponse(self):
                class _Resp(_FakeUpstreamResponse):
                    def getheaders(self):
                        return [
                            ("Content-Type", "text/plain"),
                            ("Location", "http://127.0.0.1:5173/after"),
                        ]

                r = _Resp(b"")
                r.status = 302
                return r

            def close(self):
                pass

        monkeypatch.setattr(server.http.client, "HTTPConnection", _Conn)
        probe = _RelayProbe.__new__(_RelayProbe)
        _RelayProbe.__init__(probe)
        probe.upstream_port = 5173
        probe.proxy_port = 45678
        probe._relay_http()

        sent = dict(probe.sent_headers)
        assert probe.status == 302
        assert sent["Location"] == "http://127.0.0.1:45678/after"
        assert "5173" not in sent["Location"], "the iframe would leave the proxy"

    def test_front_with_proxy_stamps_the_bound_port(self, monkeypatch):
        """The rewrite needs the proxy's REAL port, which only exists post-bind.

        Guards the wiring rather than the arithmetic: if the factory stops
        stamping `proxy_port`, `_keep_redirect_local` would rewrite redirects to
        port 0 and break the preview instead of protecting the cookie.
        """
        srv, url = server._start_inject_proxy("http://127.0.0.1:5173/")
        assert srv is not None, "proxy did not bind"
        try:
            bound = srv.RequestHandlerClass
            port = srv.server_address[1]
            assert bound.proxy_port == port
            assert bound.upstream_port == 5173
            assert url == f"http://127.0.0.1:{port}/"
        finally:
            srv.shutdown()
            srv.server_close()
