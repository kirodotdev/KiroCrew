"""Tests for the Design Tweak builtin app backend (server.py).

Focus is the security surface the static-analysis gates flagged: the single
path-containment barrier, the queue-id barrier, the child-process environment
strip (the spawned dev script must never see this backend's auth secret), the
loopback allow-list on the reverse-proxy upstream, and the upstream
`Content-Type` sanitiser that stops response splitting.
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
from pathlib import Path
from typing import Any

import pytest

if sys.platform == "win32":  # pragma: no cover - platform-gated
    import _winapi

from design_tweak_backend_helpers import isolated_queue_fixture  # noqa: F401

from conftest import requires_symlinks
from kiro_crew.apps.builtins.design_tweak.backend import server


class TestContained:
    """`_contained` is THE path sanitizer — every FS op consumes its return."""

    def test_descendant_allowed(self, tmp_path):
        (tmp_path / "a").mkdir()
        assert server._contained(tmp_path, "a") == tmp_path.resolve() / "a"

    def test_nested_relative_allowed(self, tmp_path):
        assert server._contained(tmp_path, "a/b/c.html") == tmp_path.resolve() / "a/b/c.html"

    def test_base_itself_allowed(self, tmp_path):
        assert server._contained(tmp_path) == Path(os.path.realpath(tmp_path))

    def test_dotdot_traversal_rejected(self, tmp_path):
        base = tmp_path / "proj"
        base.mkdir()
        with pytest.raises(server._PathEscape):
            server._contained(base, "../secret.txt")

    def test_deep_traversal_rejected(self, tmp_path):
        base = tmp_path / "proj"
        base.mkdir()
        with pytest.raises(server._PathEscape):
            server._contained(base, "a/../../../../etc/passwd")

    def test_absolute_candidate_rejected(self, tmp_path):
        base = tmp_path / "proj"
        base.mkdir()
        with pytest.raises(server._PathEscape):
            server._contained(base, "/etc/passwd")

    def test_sibling_prefix_rejected(self, tmp_path):
        """`/x/app-evil` starts with `/x/app` but is not inside it."""
        base = tmp_path / "app"
        base.mkdir()
        (tmp_path / "app-evil").mkdir()
        with pytest.raises(server._PathEscape):
            server._contained(base, "../app-evil/leak.txt")

    @requires_symlinks
    def test_symlink_escape_rejected(self, tmp_path):
        base = tmp_path / "proj"
        base.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        (base / "link.txt").symlink_to(outside)
        with pytest.raises(server._PathEscape):
            server._contained(base, "link.txt")


class TestRequestFile:
    def test_valid_id(self, tmp_path):
        assert server._request_file(tmp_path, "1234-abcdef").name == "1234-abcdef.json"

    @pytest.mark.parametrize("bad", ["", "../../etc/passwd", "a/b", "x\x00y", "/abs"])
    def test_bad_id_rejected(self, tmp_path, bad):
        with pytest.raises(server._PathEscape):
            server._request_file(tmp_path, bad)

    def test_read_request_outside_data_dir_returns_none(self, tmp_path):
        stray = tmp_path / "stray.json"
        stray.write_text('{"id": "x"}')
        assert server._read_request(stray) is None

    def test_write_request_outside_data_dir_raises(self, tmp_path):
        with pytest.raises(server._PathEscape):
            server._write_request(tmp_path / "stray.json", {"id": "x"})

    def test_roundtrip_inside_queue_dir(self, isolated_queue):
        fp = server._request_file(server.QUEUE_DIR, "test-roundtrip-1")
        try:
            server._write_request(fp, {"id": "test-roundtrip-1", "state": "draft"})
            assert server._read_request(fp) == {"id": "test-roundtrip-1", "state": "draft"}
        finally:
            fp.unlink(missing_ok=True)


class TestChildEnv:
    """The dev script is untrusted project code — it gets none of our secrets."""

    def test_proxy_secret_stripped(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_PROXY_SECRET", "s3cr3t-hmac-key")
        env = server._child_env(tmp_path)
        assert "KIROCREW_PROXY_SECRET" not in env
        assert "s3cr3t-hmac-key" not in "".join(env.values())

    def test_port_stripped(self, monkeypatch, tmp_path):
        # minimal_env() sets PORT to THIS backend's port; a dev server that
        # honours it would try to bind a socket we already hold.
        monkeypatch.setenv("PORT", "9110")
        assert "PORT" not in server._child_env(tmp_path)

    def test_node_options_stripped(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NODE_OPTIONS", "--max-old-space-size=99")
        assert "NODE_OPTIONS" not in server._child_env(tmp_path)

    @pytest.mark.parametrize("name", ["SSH_AUTH_SOCK", "GIT_SSH_COMMAND", "GIT_SSH"])
    def test_ssh_credentials_stripped(self, monkeypatch, tmp_path, name):
        """The dev script is untrusted project code: an inherited SSH agent
        socket or SSH override command would let it authenticate to a remote
        (`git push`, a bare `ssh`) AS the operator, with no confinement."""
        monkeypatch.setenv(name, "/tmp/agent.sock" if name == "SSH_AUTH_SOCK" else "ssh -i /secret/key")
        env = server._child_env(tmp_path)
        assert name not in env

    @pytest.mark.parametrize(
        "name",
        [
            "KIROCREW_HOME",
            "KIROCREW_APP_NAME",
            "KIROCREW_APP_DATA_DIR",
            "KIROCREW_PROJECT_DIR",
            "KIROCREW_DEVFLEET_BIN_GIT",
            "KIRO_CREW_ANYTHING",
        ],
    )
    def test_kirocrew_capability_vars_stripped(self, monkeypatch, tmp_path, name):
        monkeypatch.setenv(name, "value")
        assert name not in server._child_env(tmp_path)

    def test_ordinary_vars_survive(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", "/home/someone")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        env = server._child_env(tmp_path)
        assert env["HOME"] == "/home/someone"
        assert env["LANG"] == "en_US.UTF-8"

    def test_toolchain_dir_first_on_path(self, monkeypatch, tmp_path):
        # Build the fixture PATH with the PLATFORM separator, not a hardcoded ":".
        # `_child_env` splits and rejoins on `os.pathsep`, so a colon-joined
        # fixture stays one opaque element on Windows (pathsep is ";") and the
        # membership assertion below fails for a reason that has nothing to do
        # with the code under test. The CI matrix runs macOS, Linux AND Windows.
        existing = ["/usr/bin", "/bin"]
        monkeypatch.setenv("PATH", os.pathsep.join(existing))
        env = server._child_env(tmp_path)
        parts = env["PATH"].split(os.pathsep)
        assert parts[0] == str(tmp_path)
        for entry in existing:
            assert entry in parts


class TestValidTarget:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:5173",
            "http://127.0.0.1:3000",
            "http://LOCALHOST:8080",
            "http://localhost",  # no port at all is fine (defaults to 80)
            "http://127.0.0.1:65535",  # top of the valid range
        ],
    )
    def test_loopback_allowed(self, url):
        assert server._valid_target(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "http://evil.example.com",
            "http://169.254.169.254",  # cloud metadata — the SSRF target that matters
            "https://localhost:5173",  # scheme must be http
            "file:///etc/passwd",
            "http://127.0.0.1.evil.com",
            "http://localhost.evil.com",
            "http://[::1]:5173",
            "",
        ],
    )
    def test_everything_else_rejected(self, url):
        assert server._valid_target(url) is False

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:notaport",  # non-numeric
            "http://127.0.0.1:80a",
            "http://localhost:99999",  # out of range (urlsplit raises)
            "http://127.0.0.1:65536",  # one past the top
            "http://localhost:0",  # parses, but is not a dialable port
            "http://localhost:-1",
        ],
    )
    def test_malformed_port_rejected(self, url):
        """A bad PORT must be refused at the barrier, not at the first reader.

        `urlsplit` accepts `http://localhost:notaport` and resolves `.hostname`
        happily — `.port` is a lazily-parsed property, so the ValueError would
        surface far downstream. The URL got persisted onto the project and then
        every `/projects` poll raised while reading `.port`, turning one typo into
        a permanent 500 on project loading.
        """
        assert server._valid_target(url) is False

    def test_start_inject_proxy_survives_a_malformed_port(self):
        """Defence in depth at the other `.port` reader.

        `_start_inject_proxy` is also reached with URLs that did NOT come through
        `_valid_target` (`_auto_dev_server`, `_start_dev_proc`). It must fail to
        (None, "") — which makes the caller frame the bare URL — rather than
        raising out of the request handler.
        """
        assert server._start_inject_proxy("http://localhost:notaport") == (None, "")


class TestValidRoot:
    def test_directory_accepted(self, tmp_path):
        assert server._valid_root(str(tmp_path)) == Path(os.path.realpath(tmp_path))

    def test_file_rejected(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x")
        assert server._valid_root(str(f)) is None

    def test_traversal_normalised_away(self, tmp_path):
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        assert server._valid_root(f"{sub}/../..") == Path(os.path.realpath(tmp_path))

    @pytest.mark.parametrize("part", [".ssh", ".aws", ".gnupg", ".kube", ".docker"])
    def test_credential_dirs_rejected(self, tmp_path, part):
        d = tmp_path / part
        d.mkdir()
        assert server._valid_root(str(d)) is None

    def test_sensitive_path_floor_rejected(self, tmp_path, monkeypatch):
        """The shared floor blocks the crew home even though it is an ordinary dir."""
        monkeypatch.setattr(server, "is_sensitive_path", lambda p: True)
        assert server._valid_root(str(tmp_path)) is None

    def test_root_containing_a_credential_store_rejected(self, tmp_path, monkeypatch):
        """A root is refused for what lies UNDER it, not only what it is.

        `$HOME` is not itself a sensitive path and carries no denied component,
        so the two checks above both pass it — yet the preview servers serve
        every file below the root, which would turn `~/.ssh/id_rsa` (and a
        browser's cookie store) into a fetchable URL for the previewed page's
        own script.
        """
        monkeypatch.setattr(server, "path_contains_sensitive", lambda p: True)
        assert server._valid_root(str(tmp_path)) is None

    def test_ordinary_project_dir_still_accepted(self, tmp_path):
        """The containment check must not reject a normal project folder."""
        proj = tmp_path / "my-app"
        (proj / "src").mkdir(parents=True)
        assert server._valid_root(str(proj)) == Path(os.path.realpath(proj))


class TestClientReadTimeout:
    """Every handler must cap how long one client read may block.

    `socketserver` defaults to NO socket timeout, and `ThreadingHTTPServer`
    spends one thread plus one descriptor per connection. A client that sends a
    permitted `Content-Length` and then no body therefore parks a handler thread
    forever, and repeating it — before any HMAC check, so unauthenticated —
    exhausts the pool and the descriptor table.
    """

    @pytest.mark.parametrize(
        "handler",
        [server.Handler, server._DevProxyHandler, server._StaticInjectHandler],
    )
    def test_handler_sets_a_socket_timeout(self, handler):
        assert handler.timeout == server._CLIENT_READ_TIMEOUT
        assert isinstance(handler.timeout, (int, float)) and handler.timeout > 0

    def test_timeout_is_applied_to_the_connection(self):
        """`socketserver` only honours the attribute via `StreamRequestHandler.setup`."""
        import socketserver

        assert socketserver.StreamRequestHandler.timeout is None, (
            "the base class has no timeout, which is exactly why each handler declares one"
        )

    def _api_probe(self, headers, body):
        probe = server.Handler.__new__(server.Handler)
        probe.headers = headers
        probe.rfile = io.BytesIO(body)
        probe.close_connection = False
        return probe

    def test_short_body_raises_incomplete(self):
        probe = self._api_probe({"Content-Length": "64"}, b"nope")
        with pytest.raises(server._IncompleteBody):
            probe._read_raw_body()
        assert probe.close_connection is True, (
            "the missing bytes would be parsed as the next request on a keep-alive socket"
        )

    def test_complete_body_is_returned(self):
        probe = self._api_probe({"Content-Length": "5"}, b"hello")
        assert probe._read_raw_body() == b"hello"
        assert probe.close_connection is False

    def test_incomplete_body_is_a_valueerror_subclass(self):
        """`do_POST` catches `_IncompleteBody` before its `ValueError` arm."""
        assert issubclass(server._IncompleteBody, ValueError)


class TestStaticReadIsPinnedToTheInode:
    """The served bytes must come from an fd validated against the root.

    `_contained` proves containment by WALKING names, and the size check stats a
    NAME too — neither binds the bytes later read. `O_NOFOLLOW` guards only the
    final component, so a nested directory swapped for a symlink between the walk
    and the open escapes the approved tree and serves a file from outside it.
    """

    def test_read_goes_through_the_nolink_helper_with_the_root(self, monkeypatch, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        (root / "index.html").write_text("<html><body>ok</body></html>")
        seen: list[dict] = []

        def _spy(path, within_root=None, *, max_bytes=None, allow_truncate=False):
            seen.append({"path": path, "within_root": within_root, "max_bytes": max_bytes})
            return b"<html><body>ok</body></html>"

        monkeypatch.setattr(server, "safe_read_file_bytes_nolink", _spy)
        code, _ctype, _body = server._static_response(str(root), "index.html", "/p/")
        assert code == 200
        assert seen, "the served bytes did not go through the fd-pinned helper"
        assert seen[0]["within_root"] == str(root.resolve()), (
            "without the root the helper cannot reject an fd that resolved outside it"
        )
        assert seen[0]["max_bytes"] == server.MAX_STATIC_BYTES

    def test_a_rejected_read_is_refused_not_served_empty(self, monkeypatch, tmp_path):
        """`None` means the helper refused — serving it as empty would mask an escape."""
        root = tmp_path / "proj"
        root.mkdir()
        (root / "index.html").write_text("x")
        monkeypatch.setattr(server, "safe_read_file_bytes_nolink", lambda *a, **k: None)
        code, _ctype, body = server._static_response(str(root), "index.html", "/p/")
        assert code == 403
        assert body == b"forbidden"


class TestStopStaticPreview:
    def test_it_pops_and_shuts_down(self):
        calls: list[str] = []

        class _Srv:
            def shutdown(self):
                calls.append("shutdown")

            def server_close(self):
                calls.append("close")

        server._STATIC_SRV["p-stop"] = {"srv": _Srv(), "url": "http://127.0.0.1:1/"}
        try:
            server._stop_static_preview("p-stop")
        finally:
            server._STATIC_SRV.pop("p-stop", None)
        assert calls == ["shutdown", "close"]

    def test_an_unknown_project_is_a_no_op(self):
        server._stop_static_preview("never-registered")

    def test_a_failing_shutdown_does_not_propagate(self):
        """A remove must still answer 200 if a dying listener throws."""

        class _Bad:
            def shutdown(self):
                raise OSError("already gone")

            def server_close(self):
                pass

        server._STATIC_SRV["p-bad"] = {"srv": _Bad(), "url": ""}
        try:
            server._stop_static_preview("p-bad")
        finally:
            server._STATIC_SRV.pop("p-bad", None)
        assert "p-bad" not in server._STATIC_SRV, "a throwing listener must still be forgotten"


class TestUpstreamContentType:
    def test_known_media_type_mapped_to_literal(self):
        assert server._safe_upstream_ctype("text/html; charset=iso-8859-1", "/") == (
            "text/html; charset=utf-8"
        )

    def test_crlf_never_survives(self):
        poisoned = "text/html\r\nX-Injected: yes\r\n\r\n<script>alert(1)</script>"
        out = server._safe_upstream_ctype(poisoned, "/index.html")
        assert "\r" not in out and "\n" not in out
        assert "X-Injected" not in out

    def test_unknown_media_type_falls_back_to_extension(self):
        assert server._safe_upstream_ctype("bogus/thing", "/app.css") == "text/css"

    def test_unknown_everything_is_octet_stream(self):
        assert server._safe_upstream_ctype(None, "/thing.qqq") == "application/octet-stream"

    @pytest.mark.parametrize(
        "raw",
        ["text/html\nX: 1", "text/html\r\nX: 1", "\r\nSet-Cookie: a=b"],
    )
    def test_header_value_strips_control_chars(self, raw):
        out = server._header_value(raw)
        assert "\r" not in out and "\n" not in out

    def test_header_value_collapses_to_empty(self):
        assert server._header_value("\r\n") == ""

    def test_header_name_token_gate(self):
        assert server._HEADER_NAME_RE.match("X-Powered-By")
        assert not server._HEADER_NAME_RE.match("X-Bad: injected\r\nSet-Cookie")
        assert not server._HEADER_NAME_RE.match("X Bad")


class TestDashboardOriginRouteIsGone:
    """The `/proxy/…` route must NOT serve project content on our own origin.

    The preview iframe runs with `allow-same-origin`, which grants it its own
    loopback origin — a DIFFERENT origin from the dashboard, because ports
    separate origins under the same-origin policy even though they do not for
    cookies. So the frame cannot reach the dashboard by default.

    The one exception was this route: it served project-controlled files from the
    DASHBOARD's origin. A hostile previewed page could read that origin off
    `document.referrer` and navigate itself here, and because the document it
    then loaded was its own HTML on our origin, its script ran first-party with
    access to the authenticated API and the parent DOM. Navigating to any other
    dashboard URL gains nothing — navigation replaces the document with ours —
    so this route was the entire bridge, and it stays closed.
    """

    def test_handlers_are_deleted(self):
        for gone in ("_h_proxy", "_h_serve_root", "_h_proxy_upstream"):
            assert not hasattr(server.Handler, gone), f"{gone} came back"

    def test_route_reports_gone_and_serves_no_bytes(self, tmp_path, monkeypatch):
        (tmp_path / "index.html").write_text("<h1>secret prototype</h1>")
        monkeypatch.setattr(server, "_ROOT", str(tmp_path))
        monkeypatch.setattr(server, "_CFG", {"projects": [{"id": "abc123", "path": str(tmp_path)}]})

        class _H(server.Handler):
            def __init__(self, path):
                self.path = path
                self.sent = []
                self.headers = {}

            def _authorized(self, *_a, **_k):
                return True  # auth is not what this test is about

            def _send_raw(self, code, ctype, body):  # pragma: no cover - must not run
                raise AssertionError("the dashboard-origin route served content")

            def _json(self, code, payload):
                self.sent.append((code, payload))

        for path in ("/proxy", "/proxy/", "/proxy/abc123/index.html"):
            h = _H(path)
            server.Handler.do_GET(h)
            assert h.sent[0][0] == 410, path
            assert "secret prototype" not in str(h.sent[0][1])


class TestProxyAuthDenialIsAudited:
    """A permission denial that leaves no SEL record is indistinguishable from
    one that never happened. `file_explorer` and `md_notebook` both log
    `proxy_auth_failed`; this backend must match them."""

    def test_unsigned_request_logs_denial_then_401s(self, monkeypatch):
        calls: list[dict[str, Any]] = []

        class _Sel:
            def log_api_access(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(server, "sel", lambda: _Sel())
        monkeypatch.setattr(server, "verify_proxy_request", lambda *a, **k: False)

        class _H(server.Handler):
            def __init__(self):
                self.path = "/api/queue?projectPath=/Users/someone/secret-project"
                self.headers = {}
                self.sent: list[tuple[int, dict]] = []

            def _json(self, code, payload):
                self.sent.append((code, payload))

        h = _H()
        assert server.Handler._authorized(h, "GET", b"") is False
        assert h.sent and h.sent[0][0] == 401
        # Machine-readable code, per the non-2xx body contract in
        # docs/system-specs/common/code-style.md.
        assert h.sent[0][1].get("code") == "invalid_proxy_signature"

        assert len(calls) == 1, "the 401 emitted no audit record"
        rec = calls[0]
        assert rec["operation"] == "proxy_auth_failed"
        assert rec["outcome"] == "denied"
        # The query string can carry a project path — log the path only.
        assert rec["resources"] == "/api/queue"
        assert "secret-project" not in str(rec)

    def test_health_allowlist_is_not_audited(self, monkeypatch):
        """The gateway's own unsigned liveness probe must not spam the trail."""
        calls: list[dict[str, Any]] = []

        class _Sel:
            def log_api_access(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(server, "sel", lambda: _Sel())
        monkeypatch.setattr(server, "verify_proxy_request", lambda *a, **k: False)

        for route in ("/health", "/api/health", "/api", "/"):

            class _H(server.Handler):
                def __init__(self, p):
                    self.path = p
                    self.headers = {}
                    self.sent: list[tuple[int, dict]] = []

                def _json(self, code, payload):
                    self.sent.append((code, payload))

            h = _H(route)
            assert server.Handler._authorized(h, "GET", b"") is True
            assert h.sent == []
        assert calls == []


class TestProjectSecretsAreNeverServed:
    """A previewed project's OWN credential files must not be readable.

    `is_sensitive_path` is HOME-relative — it covers `~/.aws` and Kiro Crew's data
    home, not `<project>/.env`. The static preview is same-origin with the
    project's scripts, so `fetch('/.env')` from the page would read it back, and
    unlike a real dev server this server would happily serve any contained byte.
    """

    def test_env_and_its_suffixed_family_are_refused(self, tmp_path):
        root = tmp_path / "site"
        root.mkdir()
        (root / "index.html").write_text("<h1>x</h1>")
        for name in (".env", ".env.local", ".env.production", ".ENV"):
            (root / name).write_text("API_KEY=super-secret")
            code, _ctype, body = server._static_response(str(root), f"/{name}", "/p/")
            assert code == 403, f"{name} was served"
            assert b"super-secret" not in body

    def test_vcs_and_key_material_are_refused(self, tmp_path):
        root = tmp_path / "site"
        (root / ".git").mkdir(parents=True)
        (root / ".git" / "config").write_text("url = https://tok@github.com/x/y")
        (root / "server.pem").write_text("-----BEGIN PRIVATE KEY-----")
        (root / "sub").mkdir()
        (root / "sub" / ".npmrc").write_text("//registry:_authToken=abc")

        for rel in ("/.git/config", "/server.pem", "/sub/.npmrc"):
            code, _ctype, body = server._static_response(str(root), rel, "/p/")
            assert code == 403, f"{rel} was served"
            assert b"tok" not in body and b"PRIVATE KEY" not in body
            assert b"authToken" not in body

    def test_ordinary_project_files_still_serve(self, tmp_path):
        """The denylist must not break the preview it protects."""
        root = tmp_path / "site"
        (root / "assets").mkdir(parents=True)
        (root / "index.html").write_text("<h1>hello</h1>")
        (root / "assets" / "app.css").write_text("body{color:red}")
        # Nearby names that merely LOOK sensitive must not be caught.
        (root / "environment.js").write_text("export const x = 1")

        for rel, needle in (
            ("/index.html", b"hello"),
            ("/assets/app.css", b"color:red"),
            ("/environment.js", b"export const x"),
        ):
            code, _ctype, body = server._static_response(str(root), rel, "/p/")
            assert code == 200, f"{rel} was wrongly refused"
            assert needle in body

    def test_envrc_and_its_family_are_refused(self, tmp_path):
        """direnv's `.envrc` routinely holds `export AWS_SECRET…`.

        It is shell, not armoured key material, so the PEM content backstop
        cannot recognise it — the name check is the only thing standing between a
        preview script and those credentials. Matched as a bare `.env` prefix,
        because requiring `.env.` let `.envrc` through.
        """
        root = tmp_path / "site"
        root.mkdir()
        for name in (".envrc", ".envrc.local", ".ENVRC", ".env.production"):
            (root / name).write_text("export AWS_SECRET_ACCESS_KEY=live-value")
            code, _ctype, body = server._static_response(str(root), f"/{name}", "/p/")
            assert code == 403, f"{name} was served"
            assert b"live-value" not in body

    def test_env_prefixed_names_do_not_catch_ordinary_assets(self, tmp_path):
        """The prefix must not swallow real web files that merely start similarly."""
        root = tmp_path / "site"
        root.mkdir()
        for name in ("envelope.svg", "environment.js", "env-banner.css"):
            (root / name).write_text("/* ordinary */")
            code, _ctype, body = server._static_response(str(root), f"/{name}", "/p/")
            assert code == 200, f"{name} was wrongly refused"
            assert b"ordinary" in body

    def test_classifier_is_relative_to_the_root_not_the_absolute_path(self, tmp_path):
        """A project living under a dir called `.git` must still be previewable."""
        root = tmp_path / ".git" / "checkout" / "site"
        root.mkdir(parents=True)
        assert server._is_project_secret(root, root / "index.html") is False
        assert server._is_project_secret(root, root / ".env") is True

    def test_tls_private_key_extensions_are_refused(self, tmp_path):
        """`server.key` is the conventional TLS private-key name."""
        root = tmp_path / "site"
        root.mkdir()
        for name in ("server.key", "apple.p8", "bundle.p12", "store.jks"):
            (root / name).write_text("secret-key-material")
            code, _ctype, body = server._static_response(str(root), f"/{name}", "/p/")
            assert code == 403, f"{name} was served"
            assert b"secret-key-material" not in body

    def test_a_private_key_is_refused_whatever_it_is_called(self, tmp_path):
        """Content backstop: the extension lists cannot enumerate every name.

        A filename list is only as good as the next name someone picks, so a file
        whose bytes open with PEM private-key armour is refused regardless.
        """
        root = tmp_path / "site"
        root.mkdir()
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAxdead00beefdead00beef\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        for name in ("privkey", "id_deploy", "server.key.bak", "notes.txt", "cert.backup"):
            (root / name).write_text(pem)
            code, _ctype, body = server._static_response(str(root), f"/{name}", "/p/")
            assert code == 403, f"{name} served a private key"
            assert b"PRIVATE KEY" not in body

    def test_a_document_merely_mentioning_a_key_still_serves(self, tmp_path):
        """Only the FIRST line is inspected, so prose about keys is not blocked."""
        root = tmp_path / "site"
        root.mkdir()
        (root / "docs.html").write_text(
            "<h1>How to rotate</h1><p>Paste -----BEGIN RSA PRIVATE KEY----- here</p>"
        )
        code, _ctype, body = server._static_response(str(root), "/docs.html", "/p/")
        assert code == 200
        assert b"How to rotate" in body


class TestKiroCrewInternalTreesAreNeverServed:
    """Registering `~` must not expose Kiro Crew's OWN secrets.

    This is the hole the earlier denylist left open. `is_sensitive_path()` gates
    only the enumerated LEAVES under the crew home, so `is_sensitive_path(
    "~/.kiro/crew")` is False and everything unlisted under it was servable —
    including `<crew home>/apps/<app>/.app_secret`, the proxy-auth HMAC credential
    shared by every app backend, and `<crew home>/history/*.jsonl` chat
    transcripts. Registering `~` is explicitly a supported choice (a site at
    `~/index.html`), and the preview is same-origin with the project's scripts.
    """

    def test_app_secret_under_the_crew_home_is_refused(self, tmp_path, monkeypatch):
        """The exact reported path: `~` as root, fetch the proxy-auth credential."""
        home = tmp_path / "home"
        secret = home / ".kiro" / "crew" / "apps" / "design-tweak"
        secret.mkdir(parents=True)
        (secret / ".app_secret").write_text("hmac-credential-value")
        (home / "index.html").write_text("<h1>site</h1>")

        monkeypatch.setattr(server, "_KIROCREW_INTERNAL_DIRS", (os.path.realpath(home / ".kiro"),))

        code, _ctype, body = server._static_response(
            str(home), "/.kiro/crew/apps/design-tweak/.app_secret", "/p/"
        )
        assert code == 403
        assert b"hmac-credential-value" not in body

    def test_chat_transcripts_under_the_crew_home_are_refused(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        hist = home / ".kiro" / "crew" / "history"
        hist.mkdir(parents=True)
        (hist / "2026-08-03.jsonl").write_text('{"role":"user","content":"private"}')

        monkeypatch.setattr(server, "_KIROCREW_INTERNAL_DIRS", (os.path.realpath(home / ".kiro"),))

        code, _ctype, body = server._static_response(
            str(home), "/.kiro/crew/history/2026-08-03.jsonl", "/p/"
        )
        assert code == 403
        assert b"private" not in body

    def test_the_legacy_data_home_is_refused_too(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        legacy = home / ".kirocrew"
        legacy.mkdir(parents=True)
        (legacy / ".env").write_text("SLACK_BOT_TOKEN=xoxb-secret")

        monkeypatch.setattr(server, "_KIROCREW_INTERNAL_DIRS", (os.path.realpath(legacy),))

        code, _ctype, body = server._static_response(str(home), "/.kirocrew/.env", "/p/")
        assert code == 403
        assert b"xoxb-secret" not in body

    @requires_symlinks
    def test_a_symlink_into_the_crew_home_is_refused(self, tmp_path, monkeypatch):
        """The check realpaths, so a link inside the project cannot launder it."""
        home = tmp_path / "home"
        crew = home / ".kiro" / "crew"
        crew.mkdir(parents=True)
        (crew / "sel_hmac.key").write_text("signing-key")
        proj = tmp_path / "site"
        proj.mkdir()
        (proj / "shortcut").symlink_to(crew)

        monkeypatch.setattr(server, "_KIROCREW_INTERNAL_DIRS", (os.path.realpath(home / ".kiro"),))

        code, _ctype, body = server._static_response(str(proj), "/shortcut/sel_hmac.key", "/p/")
        assert code == 403
        assert b"signing-key" not in body

    def test_a_sibling_directory_is_not_caught(self, tmp_path, monkeypatch):
        """`~/.kiro-backup` merely shares a prefix — it must still serve."""
        home = tmp_path / "home"
        sibling = home / ".kiro-backup"
        sibling.mkdir(parents=True)
        (sibling / "notes.html").write_text("<h1>ordinary</h1>")

        monkeypatch.setattr(server, "_KIROCREW_INTERNAL_DIRS", (os.path.realpath(home / ".kiro"),))

        code, _ctype, body = server._static_response(str(home), "/.kiro-backup/notes.html", "/p/")
        assert code == 200
        assert b"ordinary" in body

    def test_app_secret_is_also_on_the_project_relative_denylist(self):
        """Defense in depth: a copy inside a project tree is refused by name."""
        assert ".app_secret" in server._PROJECT_SECRET_NAMES


class TestEntryPointCannotLaunderASecret:
    """A directory request must not serve a secret via its entry-point symlink.

    `_static_response` runs the three secret barriers on the REQUESTED path. For
    `GET /<projectId>/` that path is the directory, and the entry is chosen
    afterwards — so `index.html -> .env` passed every check and then served the
    link's target. The screen belongs inside `_find_entry`, where each candidate
    is resolved.
    """

    @requires_symlinks
    def test_index_html_symlinked_to_env_is_not_served(self, tmp_path):
        root = tmp_path / "site"
        root.mkdir()
        (root / ".env").write_text("STRIPE_KEY=live-secret")
        (root / "index.html").symlink_to(root / ".env")

        code, _ctype, body = server._static_response(str(root), "/", "/p/")
        assert b"live-secret" not in body
        assert code != 200

    @requires_symlinks
    def test_entry_symlinked_into_the_crew_home_is_not_served(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        crew = home / ".kiro" / "crew"
        crew.mkdir(parents=True)
        (crew / "sel_hmac.key").write_text("signing-key")
        root = tmp_path / "site"
        root.mkdir()
        (root / "index.html").symlink_to(crew / "sel_hmac.key")

        monkeypatch.setattr(server, "_KIROCREW_INTERNAL_DIRS", (os.path.realpath(home / ".kiro"),))

        code, _ctype, body = server._static_response(str(root), "/", "/p/")
        assert b"signing-key" not in body
        assert code != 200

    @requires_symlinks
    def test_find_entry_skips_a_secret_and_keeps_looking(self, tmp_path):
        """A later legitimate candidate must still be found."""
        root = tmp_path / "site"
        (root / "public").mkdir(parents=True)
        (root / ".env").write_text("SECRET=1")
        (root / "index.html").symlink_to(root / ".env")
        (root / "public" / "index.html").write_text("<h1>real entry</h1>")

        entry = server._find_entry(root, root)
        assert entry is not None
        assert entry.read_text() == "<h1>real entry</h1>"

    def test_an_ordinary_entry_is_unaffected(self, tmp_path):
        root = tmp_path / "site"
        root.mkdir()
        (root / "index.html").write_text("<h1>hello</h1>")

        code, _ctype, body = server._static_response(str(root), "/", "/p/")
        assert code == 200
        assert b"hello" in body


class TestHtmlScanDoesNotFollowSymlinks:
    """The diagnostic 404 lists what `_scan_html` finds, so its walk discloses names.

    `e.is_dir()` follows a symlink, so `docs -> ~/.ssh` in a project with no entry
    page would have the walk enumerate a protected directory and print the HTML
    filenames inside it. Contents never leak — the listing does, which is what the
    sensitive-path floor exists to withhold.
    """

    @requires_symlinks
    def test_a_symlinked_directory_is_not_enumerated(self, tmp_path):
        secret = tmp_path / "protected"
        secret.mkdir()
        (secret / "private-notes.html").write_text("<h1>secret</h1>")
        root = tmp_path / "site"
        root.mkdir()
        (root / "docs").symlink_to(secret)
        (root / "real.html").write_text("<h1>ok</h1>")

        found = server._scan_html(root)
        assert "real.html" in found
        assert not any("private-notes" in f for f in found), found

    @requires_symlinks
    def test_a_symlinked_file_is_not_listed(self, tmp_path):
        secret = tmp_path / "protected"
        secret.mkdir()
        (secret / "leak.html").write_text("<h1>secret</h1>")
        root = tmp_path / "site"
        root.mkdir()
        (root / "alias.html").symlink_to(secret / "leak.html")
        (root / "real.html").write_text("<h1>ok</h1>")

        found = server._scan_html(root)
        assert found == ["real.html"], found

    @requires_symlinks
    def test_the_404_page_cannot_disclose_a_symlinked_tree(self, tmp_path):
        """End-to-end: a project with NO entry page renders the diagnostic listing."""
        secret = tmp_path / "protected"
        secret.mkdir()
        (secret / "private-notes.html").write_text("<h1>secret</h1>")
        root = tmp_path / "site"
        root.mkdir()
        (root / "docs").symlink_to(secret)

        code, _ctype, body = server._static_response(str(root), "/", "/p/")
        assert code == 404
        assert b"private-notes" not in body

    def test_a_junctioned_directory_is_not_enumerated(self, tmp_path, monkeypatch):
        """A junction is the case that actually reaches a Windows user.

        Every other test in this class is `@requires_symlinks` and therefore skipped
        on Windows, because a symlink there needs a privilege. A junction needs none
        — so the one link type a Windows user can plant was the one the guard did not
        cover. `is_symlink()` does not report a junction and a junction IS a
        directory, so the walk fell to the `is_dir()` arm and listed the linked tree.
        """
        secret = tmp_path / "protected"
        secret.mkdir()
        (secret / "private-notes.html").write_text("<h1>secret</h1>")
        root = tmp_path / "site"
        root.mkdir()
        link = root / "docs"
        link.mkdir()
        (link / "private-notes.html").write_text("<h1>secret</h1>")
        (root / "real.html").write_text("<h1>ok</h1>")

        monkeypatch.setattr(server, "is_link_or_junction", lambda p: Path(p) == link)
        found = server._scan_html(root)

        assert "real.html" in found
        assert not any("private-notes" in f for f in found), found

    def test_the_scan_consults_the_shared_link_helper(self, tmp_path, monkeypatch):
        """A junction is only refused if the walk ASKS the shared helper about it —
        the seam, not the outcome, is what `is_symlink()` got wrong."""
        root = tmp_path / "site"
        (root / "public").mkdir(parents=True)
        (root / "public" / "a.html").write_text("x")
        seen = []

        def _spy(p):
            seen.append(Path(p).name)
            return False

        monkeypatch.setattr(server, "is_link_or_junction", _spy)
        server._scan_html(root)

        assert "public" in seen

    @pytest.mark.skipif(
        sys.platform != "win32", reason="junctions are a Windows reparse point"
    )
    def test_a_real_windows_junction_is_not_enumerated(self, tmp_path):
        """No stub and no elevation: the real reparse point, so the stubbed tests
        above stand in for something rather than for nothing."""
        secret = tmp_path / "protected"
        secret.mkdir()
        (secret / "private-notes.html").write_text("<h1>secret</h1>")
        root = tmp_path / "site"
        root.mkdir()
        (root / "real.html").write_text("<h1>ok</h1>")
        _winapi.CreateJunction(str(secret), str(root / "docs"))

        found = server._scan_html(root)

        assert "real.html" in found
        assert not any("private-notes" in f for f in found), found

    def test_ordinary_nested_html_is_still_found(self, tmp_path):
        """The refusal must not break the diagnostic page it feeds."""
        root = tmp_path / "site"
        (root / "public" / "deep").mkdir(parents=True)
        (root / "index.htm").write_text("x")
        (root / "public" / "a.html").write_text("x")
        (root / "public" / "deep" / "b.html").write_text("x")

        found = server._scan_html(root)
        assert set(found) == {"index.htm", "public/a.html", "public/deep/b.html"}


class TestStaticPreviewIsSizeBounded:
    """One oversized asset must not be able to take the backend down.

    `_static_response` buffers the whole file — there is no streaming path — so a
    video or archive sitting in the previewed project would be materialised in
    memory in one go. The project is the user's own, so this is an accident rather
    than an attack, but the outcome is the same: the app dies mid-preview.
    """

    def test_a_file_over_the_ceiling_is_refused_without_reading_it(self, tmp_path, monkeypatch):
        root = tmp_path / "site"
        root.mkdir()
        big = root / "trailer.mp4"
        big.write_bytes(b"x" * 4096)
        monkeypatch.setattr(server, "MAX_STATIC_BYTES", 1024)

        # Fail loudly if the guard reads the file rather than stat-ing it.
        def _boom(*_a, **_k):
            raise AssertionError("read_bytes() was called on an oversized file")

        monkeypatch.setattr(server.Path, "read_bytes", _boom)

        code, _ctype, body = server._static_response(str(root), "/trailer.mp4", "/p/")
        assert code == 413
        assert b"preview limit" in body

    def test_a_file_at_the_ceiling_still_serves(self, tmp_path, monkeypatch):
        """The bound is inclusive — exactly-at-limit is not oversized."""
        root = tmp_path / "site"
        root.mkdir()
        (root / "app.css").write_bytes(b"y" * 1024)
        monkeypatch.setattr(server, "MAX_STATIC_BYTES", 1024)

        code, _ctype, body = server._static_response(str(root), "/app.css", "/p/")
        assert code == 200
        assert len(body) == 1024

    def test_the_default_ceiling_is_generous_enough_for_real_assets(self):
        """A too-tight cap would break ordinary previews, so pin the magnitude."""
        assert server.MAX_STATIC_BYTES >= 16 * 1024 * 1024

    def test_ordinary_files_are_unaffected(self, tmp_path):
        root = tmp_path / "site"
        root.mkdir()
        (root / "index.html").write_text("<h1>hello</h1>")
        code, _ctype, body = server._static_response(str(root), "/index.html", "/p/")
        assert code == 200
        assert b"hello" in body


class TestDataHomeDefault:
    def test_default_home_is_kiro_crew(self, tmp_path, monkeypatch):
        """With no env override the data dir lands under ~/.kiro/crew.

        The pre-move `~/.kirocrew` home is dead; loading a fresh copy of the
        module proves the fallback, rather than asserting on source text.
        """
        for var in ("KIROCREW_APP_DATA_DIR", "KIROCREW_APP_DATA", "KIROCREW_HOME"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path), 1))

        spec = importlib.util.spec_from_file_location(
            "_design_tweak_server_home_probe", server.__file__
        )
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        expected = (tmp_path / ".kiro" / "crew" / "apps" / mod.APP_NAME / "data").resolve()
        assert mod.DATA_DIR == expected
        assert ".kirocrew" not in str(mod.DATA_DIR)


class TestGuessCtypeIsConstant:
    """A disk-served `Content-Type` must be a CONSTANT, never derived from the path.

    The requested path is attacker-influenced, so computing a header value from it
    (`mimetypes.guess_type(path)`) put a tainted string one step from
    `send_header` — `py/http-response-splitting`. A closed lookup that can only
    return a literal breaks the flow instead of trying to sanitise it, and also
    stops the result varying with the host's `/etc/mime.types`.
    """

    def test_every_mapped_value_is_a_literal_from_the_table(self):
        literals = set(server._CTYPE_OVERRIDES.values()) | {server._CTYPE_DEFAULT}
        for ext in server._CTYPE_OVERRIDES:
            assert server._guess_ctype(Path("a" + ext)) in literals

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("index.html", "text/html"),
            ("app.JS", "text/javascript"),
            ("style.css", "text/css"),
            ("logo.svg", "image/svg+xml"),
            ("data.json", "application/json"),
            ("font.woff2", "font/woff2"),
        ],
    )
    def test_known_extensions(self, name, expected):
        assert server._guess_ctype(Path(name)) == expected

    def test_unknown_extension_is_inert(self):
        assert server._guess_ctype(Path("x.weird")) == "application/octet-stream"
        assert server._guess_ctype(Path("noext")) == "application/octet-stream"

    def test_crlf_in_the_filename_cannot_reach_the_header(self):
        """The name is only a lookup key, so nothing from it is echoed back."""
        evil = Path("a.css\r\nSet-Cookie: x=1")
        got = server._guess_ctype(evil)
        assert "\r" not in got and "\n" not in got
        assert got == "application/octet-stream"

    def test_upstream_ctype_still_maps_for_the_proxied_path(self):
        """The proxy path keeps its own selector — it must reflect the upstream."""
        assert server._safe_upstream_ctype("text/html; charset=iso-8859-1", "/x") == (
            "text/html; charset=utf-8"
        )
        assert server._safe_upstream_ctype("evil/\r\nSet-Cookie: a=b", "/x.css") == ("text/css")
        assert server._safe_upstream_ctype(None, "/x.unknown") == "application/octet-stream"


class TestStaticResponse:
    """`_static_response` is the ONE static-serving rulebook.

    Two servers render a project folder — the gateway-proxied route and the
    loopback preview server the iframe is actually pointed at — and they must
    agree on containment, entry-point resolution, overlay injection and the
    diagnostic pages. These assert the shared builder, so a divergence between
    the two callers cannot reintroduce a hole in only one of them.
    """

    def test_serves_index_and_injects_overlay(self, tmp_path):
        (tmp_path / "index.html").write_text("<html><body><h1>hi</h1></body></html>")
        status, ctype, body = _static_response_at(tmp_path, "")
        assert status == 200
        assert "text/html" in ctype
        assert b"<h1>hi</h1>" in body
        assert server._OVERLAY_PATH.encode() in body

    def test_resolves_nested_entry_candidate(self, tmp_path):
        """A project whose site lives in app/ still previews (`_ENTRY_CANDIDATES`)."""
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "index.html").write_text("<html><body>nested</body></html>")
        status, _, body = _static_response_at(tmp_path, "")
        assert status == 200
        assert b"nested" in body
        # <base> must point at the served file's own directory, or its relative
        # assets resolve one level too high and the page renders blank.
        assert b'<base href="/p1/app/"' in body

    def test_traversal_is_refused(self, tmp_path):
        (tmp_path / "proj").mkdir()
        (tmp_path / "secret.txt").write_text("token")
        status, _, body = _static_response_at(tmp_path / "proj", "../secret.txt")
        assert status == 403
        assert b"token" not in body

    def test_absolute_path_is_not_served(self, tmp_path):
        """A leading slash is stripped, so it can only ever resolve INSIDE root.

        `/etc/passwd` becomes `<root>/etc/passwd`, which does not exist — hence a
        404 rather than a 403. What matters is that the host's file is never read;
        `_contained` is the backstop for the forms the strip does not neutralise.
        """
        (tmp_path / "proj").mkdir()
        status, _, body = _static_response_at(tmp_path / "proj", "/etc/passwd")
        assert status != 200
        assert b"root:" not in body

    def test_missing_entry_renders_diagnostic_404(self, tmp_path):
        """The diagnostic page is preserved behaviour, not a bare 'not found'."""
        (tmp_path / "pages").mkdir()
        (tmp_path / "pages" / "about.html").write_text("<html><body>about</body></html>")
        status, ctype, body = _static_response_at(tmp_path, "")
        assert status == 404
        assert "text/html" in ctype
        # It lists the HTML it DID find, as a link through this server's own base.
        assert b"/p1/pages/about.html" in body

    def test_bundler_template_explains_instead_of_blank_page(self, tmp_path):
        (tmp_path / "index.html").write_text(
            '<html><body><div id="root"></div>'
            '<script type="module" src="/src/main.tsx"></script></body></html>'
        )
        status, _, body = _static_response_at(tmp_path, "")
        # 200: the file was found and read fine — the page IS the answer.
        assert status == 200
        assert b"needs a dev server" in body
        assert b"main.tsx" in body

    def test_non_html_is_served_verbatim(self, tmp_path):
        (tmp_path / "app.css").write_text("body{color:red}")
        status, ctype, body = _static_response_at(tmp_path, "app.css")
        assert status == 200
        assert "css" in ctype
        assert body == b"body{color:red}"
        # No overlay tag smuggled into a non-HTML body.
        assert server._OVERLAY_PATH.encode() not in body


def _static_response_at(root, rel):
    """Call the shared builder the way the loopback preview server does."""
    return server._static_response(str(root), rel, "/p1/", script=server._OVERLAY_PATH)


@pytest.fixture()
def static_preview(tmp_path, monkeypatch):
    """A live loopback static preview server with one registered project."""
    import urllib.error
    import urllib.request

    root = tmp_path / "proj"
    root.mkdir()
    (root / "index.html").write_text("<html><body><h1>preview</h1></body></html>")
    (root / "style.css").write_text("h1{color:red}")
    (tmp_path / "outside.txt").write_text("do-not-serve")

    monkeypatch.setitem(server._CFG, "projects", [{"id": "p1", "path": str(root)}])
    monkeypatch.setattr(server, "_STATIC_SRV", {})

    base = server._static_preview_base("p1")
    assert base, "static preview server did not bind"

    def get(path, headers=None, method="GET"):
        req = urllib.request.Request(base.rstrip("/") + path, headers=headers or {}, method=method)
        try:
            # `base` is this test's own loopback server URL from
            # `_static_preview_base()` (http://127.0.0.1:<ephemeral>), and `path`
            # is a literal from the test body. No external input reaches it.
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.getheaders()), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    try:
        yield base, root, get
    finally:
        rec = server._STATIC_SRV.get("p1")
        srv = rec.get("srv") if rec else None
        if srv is not None:
            srv.shutdown()
            srv.server_close()


class TestStaticPreviewServer:
    """The loopback server the preview iframe is pointed at.

    It exists so the framed project does NOT share the dashboard's origin —
    `allow-scripts` + `allow-same-origin` on our own origin would cancel the
    sandbox. That makes it an unauthenticated listener by design, so the
    properties asserted here are the whole of its containment.
    """

    def test_binds_loopback_only(self, static_preview):
        _base, _root, _get = static_preview
        srv = server._STATIC_SRV["p1"]["srv"]
        assert srv.server_address[0] == "127.0.0.1"
        assert str(server._STATIC_SRV["p1"]["url"]).startswith("http://127.0.0.1:")

    def test_url_is_not_the_dashboard_origin(self, static_preview):
        """The entire point: a distinct origin from the gateway-proxied route."""
        base, _root, _get = static_preview
        assert not base.startswith(server.PROXY_PUBLIC_BASE)
        assert "/apps/" not in base

    def test_per_project_url_shape(self, static_preview):
        base, _root, _get = static_preview
        assert server._static_preview_url("p1") == f"{base}p1/"

    def test_serves_the_overlay_at_the_overlay_path(self, static_preview):
        _base, _root, get = static_preview
        status, headers, body = get(server._OVERLAY_PATH)
        assert status == 200
        assert "javascript" in headers["Content-Type"]
        # The real overlay, not a stub.
        assert b"kiro-select-to-edit" in body

    def test_injects_the_overlay_into_html(self, static_preview):
        _base, _root, get = static_preview
        status, headers, body = get("/p1/")
        assert status == 200
        assert "text/html" in headers["Content-Type"]
        assert b"<h1>preview</h1>" in body
        assert f'<script src="{server._OVERLAY_PATH}">'.encode() in body

    def test_cannot_serve_outside_the_project_root(self, static_preview):
        """Routed through the same `_contained` barrier as every other read."""
        _base, _root, get = static_preview
        for attempt in ("/p1/../outside.txt", "/p1/%2e%2e/outside.txt"):
            status, _headers, body = get(attempt)
            assert status in (403, 404), attempt
            assert b"do-not-serve" not in body, attempt

    def test_unknown_project_is_refused(self, static_preview):
        _base, _root, get = static_preview
        status, _headers, _body = get("/nope/index.html")
        assert status == 404

    def test_is_read_only(self, static_preview):
        _base, _root, get = static_preview
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            status, _headers, _body = get("/p1/", method=method)
            assert status == 405, method

    def test_forwards_no_credentials_and_sets_none(self, static_preview):
        """There is no upstream to forward to, and no cookie is ever issued."""
        _base, _root, get = static_preview
        status, headers, body = get(
            "/p1/",
            headers={"Cookie": "kirocrew_token=super-secret", "Authorization": "Bearer x"},
        )
        assert status == 200
        assert b"super-secret" not in body
        assert "Set-Cookie" not in headers
        # No CORS grant: the previewed project must not become a readable
        # cross-origin API for some other page.
        assert "Access-Control-Allow-Origin" not in headers

    def test_has_no_relay_path_at_all(self):
        """Structural: unlike the dev proxy there is nothing to leak headers to."""
        assert not hasattr(server._StaticInjectHandler, "_relay_http")
        assert not hasattr(server._StaticInjectHandler, "_relay_ws")

    def test_two_projects_never_share_a_browser_storage_origin(self, static_preview, tmp_path):
        """Same-origin is scheme+host+PORT — a shared path prefix is not enough.

        A second project registered alongside the fixture's "p1" must bind its
        OWN listener on its OWN port, not be served by "p1"'s. If it were,
        both projects' previews would share one localStorage/cookie jar: a
        page in project B could read whatever project A's page stored there,
        with the URL path being the only thing that ever distinguished them.
        """
        base_p1, _root, _get = static_preview
        root2 = tmp_path / "proj2"
        root2.mkdir()
        (root2 / "index.html").write_text("<html><body>two</body></html>")
        server._CFG["projects"].append({"id": "p2", "path": str(root2)})
        try:
            base_p2 = server._static_preview_base("p2")
            assert base_p2, "second project's static preview server did not bind"
            assert base_p2 != base_p1, "two projects were handed the SAME origin"
        finally:
            rec = server._STATIC_SRV.get("p2")
            srv = rec.get("srv") if rec else None
            if srv is not None:
                srv.shutdown()
                srv.server_close()

    def test_a_project_s_own_server_refuses_another_project_s_path(self, static_preview, tmp_path):
        """Even reaching the WRONG listener by IP:port cannot cross projects.

        Belt-and-suspenders on top of the origin split above: "p1"'s own
        listener must refuse a request naming any other project id, not just
        happen to never receive one.
        """
        _base, _root, get = static_preview
        status, _headers, _body = get("/p2/index.html")
        assert status == 404


class TestCustomCrewHomeIsRefused:
    """A relocated crew home must be refused as a whole tree.

    `DATA_DIR` only covers THIS app's subtree, so with a custom home the rest of
    it — the chat transcripts under `history/`, `sessions.db`, the governance
    policy files — sat outside every internal-dir entry. Registering a project
    whose root contains that home then put them one same-origin `fetch()` from any
    script on the previewed page. Refusing the tree closes the class rather than
    the two filenames we happened to think of.
    """

    def test_default_home_is_covered_by_the_kiro_entry(self):
        home = Path(os.path.realpath(os.path.expanduser("~")))
        assert server._is_kirocrew_internal(home / ".kiro" / "crew" / "history" / "x.jsonl")

    def test_the_resolved_crew_home_is_listed(self):
        """Structural: the resolution must land in the tuple, not hold by accident."""
        listed = {os.path.realpath(p) for p in server._KIROCREW_INTERNAL_DIRS}
        assert os.path.realpath(server._CREW_HOME) in listed

    def test_a_sibling_of_the_home_is_not_over_blocked(self):
        """Separator-aware: `~/.kiro-backup` is a user directory, not ours."""
        home = Path(os.path.realpath(os.path.expanduser("~")))
        assert not server._is_kirocrew_internal(home / ".kiro-backup" / "notes.md")

    def test_custom_home_resolution_honours_the_env_var(self, tmp_path, monkeypatch):
        """Reload the module under a relocated home and check the tree is refused.

        The tuple is built at import time from the environment, so re-importing is
        the only honest way to exercise the relocated-home branch.
        """
        import importlib

        relocated = tmp_path / "relocated-crew"
        (relocated / "history").mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(relocated))
        monkeypatch.delenv("KIROCREW_APP_DATA_DIR", raising=False)
        monkeypatch.delenv("KIROCREW_APP_DATA", raising=False)

        reloaded = importlib.reload(server)
        try:
            assert reloaded._is_kirocrew_internal(relocated / "history" / "chat.jsonl")
            assert reloaded._is_kirocrew_internal(relocated / "sessions.db")
        finally:
            # Restore the module for every later test in the session.
            monkeypatch.undo()
            importlib.reload(server)


class TestProjectSecretDirs:
    """Credential DIRECTORIES inside a previewed project are never served."""

    def test_container_registry_dir_is_listed(self):
        """`.docker/config.json` carries registry auth in its `auths` entries."""
        assert ".docker" in server._PROJECT_SECRET_DIRS

    @pytest.mark.parametrize(
        "rel",
        [
            ".docker/config.json",
            "nested/.docker/config.json",
            ".aws/credentials",
            ".kube/config",
        ],
    )
    def test_secret_dirs_are_matched_on_any_component(self, tmp_path, rel):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("secret", "utf-8")
        assert server._is_project_secret(tmp_path, target), rel

    def test_an_ordinary_asset_is_still_served(self, tmp_path):
        target = tmp_path / "assets" / "app.js"
        target.parent.mkdir(parents=True)
        target.write_text("console.log(1)", "utf-8")
        assert not server._is_project_secret(tmp_path, target)


class TestStaticCredentialBackstop:
    """A credential in an ORDINARY project file must not reach the previewed page.

    The previewed page's own JavaScript is same-origin with the static server, so
    it can fetch any file under the project root. A filename denylist cannot cover
    a key checked into `config.js`, so the gate is on content.
    """

    def _serve(self, root, name, content):
        (root / name).write_bytes(content)
        return server._static_response(str(root), name, "/")

    def test_labelled_credential_in_ordinary_file_is_refused(self, tmp_path):
        status, _, _ = self._serve(
            tmp_path, "config.js", b"export const KEY = 'AKIA" + b"A" * 16 + b"';\n"
        )
        assert status == 403

    def test_vendor_prefixed_token_is_refused(self, tmp_path):
        status, _, _ = self._serve(tmp_path, "notes.md", b"token: sk-ant-" + b"x" * 20 + b"\n")
        assert status == 403

    def test_ordinary_asset_is_still_served(self, tmp_path):
        status, _, body = self._serve(tmp_path, "app.js", b"export const n = 1;\n")
        assert status == 200
        assert b"export const n = 1;" in body

    def test_binary_asset_passes_through(self, tmp_path):
        # Not decodable as UTF-8, so there is no text to scan. Refusing binary
        # assets would blank every preview that loads an image or a font.
        png = b"\x89PNG\r\n\x1a\n" + bytes(range(200, 256))
        status, _, body = self._serve(tmp_path, "logo.png", png)
        assert status == 200
        assert body == png

    def test_high_entropy_hash_is_not_refused(self, tmp_path):
        # Pins the DELIBERATE scope of the gate. `redact_credentials()` also runs an
        # entropy-gated hunt for bare 40-char secrets, which security.py calls its
        # highest-false-positive rule; using it here would refuse bundles and
        # hash-like runs, and a refused asset renders the preview blank. Only the
        # labelled / vendor-prefixed classes are gated.
        bundle = b"//# sourceMappingURL=app." + b"a1b2c3d4e5" * 4 + b".js.map\n"
        status, _, _ = self._serve(tmp_path, "bundle.js", bundle)
        assert status == 200

    def test_real_key_embedded_mid_file_is_refused(self, tmp_path):
        # Why the PEM class stays in the CONTENT scan and not just the first-line
        # check: armoured key material pasted into a JSON fixture starts at line 3,
        # so a first-line-only test serves it verbatim.
        blob = (
            b'{\n  "note": "deploy key",\n  "pem": "-----BEGIN RSA PRIVATE KEY-----\\n'
            b"MIIEowIBAAKCAQEAxdead00beefdead00beefMIIEowIBAAKCAQEA\\n"
            b'-----END RSA PRIVATE KEY-----"\n}\n'
        )
        status, _, _ = self._serve(tmp_path, "fixture.json", blob)
        assert status == 403

    def test_prose_quoting_a_pem_marker_still_serves(self, tmp_path):
        # The counterpart constraint, already pinned elsewhere for the first-line
        # check: a marker with no body is a label. Refusing it would blank a
        # legitimate docs page while leaking nothing.
        doc = b"<h1>Rotate</h1><p>Paste -----BEGIN RSA PRIVATE KEY----- here</p>"
        status, _, body = self._serve(tmp_path, "docs.html", doc)
        assert status == 200
        assert b"Rotate" in body
