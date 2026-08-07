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
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.design_tweak.backend import server
from kiro_crew.platform_compat import IS_POSIX


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

    def test_roundtrip_inside_queue_dir(self):
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
        happily — `.port` is a lazily-parsed property, so the ValueError used to
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


class TestPreviewSuppliedSourcePathsAreContained:
    """A previewed page must not aim the agent's edit outside the project.

    `previewUrl` and each element's `source` block are produced INSIDE the
    previewed page, so both are attacker-controlled when the previewed project
    is hostile. Neither value is opened by this backend — they are handed to the
    agent as "the exact source file to edit", so the barrier protects the edit
    TARGET, and an unchecked `..` would point a legitimate agent at a file
    outside the registered root.
    """

    def test_traversal_in_preview_url_yields_no_source_file(self, tmp_path, monkeypatch):
        proj = tmp_path / "site"
        proj.mkdir()
        cfg = {
            "projects": [{"id": "p1", "path": str(proj), "name": "site"}],
            "activeId": "p1",
            "counter": 0,
        }
        monkeypatch.setattr(server, "_CFG", cfg)
        monkeypatch.setattr(server, "_ROOT", str(proj))

        pid, root, source_file = server._resolve_project(
            {
                "projectId": "p1",
                "previewUrl": "/apps/design-tweak/api/proxy/p1/../../../../etc/passwd",
            }
        )
        assert pid == "p1"
        assert root == str(proj)
        # Escaped -> cleared, NOT joined into a path outside the project.
        assert source_file == ""
        assert "etc/passwd" not in source_file

    def test_ordinary_served_file_still_resolves(self, tmp_path, monkeypatch):
        """The barrier must not break the normal case it guards."""
        proj = tmp_path / "site"
        (proj / "sub").mkdir(parents=True)
        (proj / "sub" / "index.html").write_text("<h1>x</h1>")
        cfg = {
            "projects": [{"id": "p1", "path": str(proj), "name": "site"}],
            "activeId": "p1",
            "counter": 0,
        }
        monkeypatch.setattr(server, "_CFG", cfg)
        monkeypatch.setattr(server, "_ROOT", str(proj))

        _pid, _root, source_file = server._resolve_project(
            {"projectId": "p1", "previewUrl": "/apps/design-tweak/api/proxy/p1/sub/index.html"}
        )
        assert source_file == str(Path(os.path.realpath(proj)) / "sub" / "index.html")

    def test_symlink_escape_in_preview_url_is_rejected(self, tmp_path, monkeypatch):
        """A symlink inside the project pointing out of it must not resolve.

        `_contained` realpaths BOTH sides, so the link is followed before the
        containment test — a served path is judged by where it lands, not how it
        is spelled.
        """
        proj = tmp_path / "site"
        proj.mkdir()
        secret = tmp_path / "outside"
        secret.mkdir()
        (secret / "creds.env").write_text("TOKEN=1")
        (proj / "escape").symlink_to(secret)

        cfg = {
            "projects": [{"id": "p1", "path": str(proj), "name": "site"}],
            "activeId": "p1",
            "counter": 0,
        }
        monkeypatch.setattr(server, "_CFG", cfg)
        monkeypatch.setattr(server, "_ROOT", str(proj))

        _pid, _root, source_file = server._resolve_project(
            {
                "projectId": "p1",
                "previewUrl": "/apps/design-tweak/api/proxy/p1/escape/creds.env",
            }
        )
        assert source_file == ""

    def test_absolute_path_in_preview_url_is_rejected(self, tmp_path, monkeypatch):
        """An absolute `served_rel` wins `os.path.join`; containment must catch it."""
        proj = tmp_path / "site"
        proj.mkdir()
        cfg = {
            "projects": [{"id": "p1", "path": str(proj), "name": "site"}],
            "activeId": "p1",
            "counter": 0,
        }
        monkeypatch.setattr(server, "_CFG", cfg)
        monkeypatch.setattr(server, "_ROOT", str(proj))

        _pid, _root, source_file = server._resolve_project(
            {"projectId": "p1", "previewUrl": "/apps/design-tweak/api/proxy/p1//etc/passwd"}
        )
        assert source_file == ""

    def test_element_source_escape_is_cleared_and_downgraded(self, tmp_path):
        """A hostile `data-kiro-source` cannot name a file outside the root."""
        proj = tmp_path / "site"
        proj.mkdir()
        sel: dict[str, Any] = {
            "mode": "single",
            "elements": [
                {
                    "locator": "div.a",
                    "source": {"file": "../../../../etc/hosts", "line": 1, "confidence": "high"},
                },
                {
                    "locator": "div.b",
                    "source": {"file": "/etc/passwd", "line": 2, "confidence": "high"},
                },
            ],
        }
        server._sanitize_selection_sources(sel, str(proj))

        for el in sel["elements"]:
            assert el["source"]["file"] == ""
            # Cleared with a stale "high" would be an incoherent hint.
            assert el["source"]["confidence"] == "low"
            assert el["locator"]  # the usable anchor is untouched

    def test_element_source_inside_root_survives(self, tmp_path):
        """React Fiber reports ABSOLUTE paths under the project — keep them."""
        proj = tmp_path / "site"
        (proj / "src").mkdir(parents=True)
        real = Path(os.path.realpath(proj)) / "src" / "App.tsx"
        sel: dict[str, Any] = {
            "elements": [
                {"source": {"file": str(real), "line": 7, "confidence": "medium"}},
                {"source": {"file": "src/App.tsx", "line": 7, "confidence": "high"}},
            ]
        }
        server._sanitize_selection_sources(sel, str(proj))

        assert sel["elements"][0]["source"]["file"] == str(real)
        assert sel["elements"][0]["source"]["confidence"] == "medium"
        assert sel["elements"][1]["source"]["file"] == str(real)
        assert sel["elements"][1]["source"]["confidence"] == "high"

    def test_no_known_root_fails_closed(self):
        """With nothing to contain against, no hint is trusted."""
        sel: dict[str, Any] = {
            "elements": [{"source": {"file": "src/App.tsx", "confidence": "high"}}]
        }
        server._sanitize_selection_sources(sel, "")
        assert sel["elements"][0]["source"]["file"] == ""
        assert sel["elements"][0]["source"]["confidence"] == "low"

    def test_malformed_selection_shapes_are_tolerated(self):
        """Never raise on a shape the preview page controls."""
        for bad in (
            None,
            [],
            "x",
            {"elements": None},
            {"elements": ["x", None, {}]},
            {"elements": [{"source": "notadict"}, {"source": {}}]},
        ):
            server._sanitize_selection_sources(bad, "/tmp")

    def test_submit_routes_the_selection_through_the_barrier(self):
        """The sanitizer is actually WIRED into the submit path, not just present."""
        import inspect

        src = inspect.getsource(server.Handler._h_submit)
        assert "_sanitize_selection_sources" in src


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
        # Machine-readable code, per AGENTS.md's non-2xx body contract.
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

    def test_the_fallback_dev_url_return_is_gone_from_the_source(self):
        """Pin the barrier: no `return dev_url` may creep back into the helper."""
        import inspect

        src = inspect.getsource(server._front_with_proxy)
        assert "return dev_url" not in src
        assert 'return ""' in src


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


class TestDraftCommentCap:
    """A previewed page drives captures, so the draft must be bounded.

    Nothing reaches the agent without a separate Send, so the exposure is
    unbounded growth of the user's own queue file rather than an agent action —
    a cap bounds it without requiring a parent-side gesture, which would break
    the in-preview interaction the product exists for.
    """

    def test_cap_is_enforced_and_reports_a_code(self):
        import inspect

        src = inspect.getsource(server.Handler._h_submit)
        assert "MAX_DRAFT_COMMENTS" in src, "the draft cap is not enforced on submit"
        assert "draft_comment_limit" in src, "the refusal carries no machine-readable code"
        assert server.MAX_DRAFT_COMMENTS > 0


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

    def test_the_barrier_is_wired_into_the_static_sink(self):
        import inspect

        src = inspect.getsource(server._static_response)
        assert "_is_kirocrew_internal" in src


class TestEntryPointCannotLaunderASecret:
    """A directory request must not serve a secret via its entry-point symlink.

    `_static_response` runs the three secret barriers on the REQUESTED path. For
    `GET /<projectId>/` that path is the directory, and the entry is chosen
    afterwards — so `index.html -> .env` passed every check and then served the
    link's target. The screen belongs inside `_find_entry`, where each candidate
    is resolved.
    """

    def test_index_html_symlinked_to_env_is_not_served(self, tmp_path):
        root = tmp_path / "site"
        root.mkdir()
        (root / ".env").write_text("STRIPE_KEY=live-secret")
        (root / "index.html").symlink_to(root / ".env")

        code, _ctype, body = server._static_response(str(root), "/", "/p/")
        assert b"live-secret" not in body
        assert code != 200

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

    def test_ordinary_nested_html_is_still_found(self, tmp_path):
        """The refusal must not break the diagnostic page it feeds."""
        root = tmp_path / "site"
        (root / "public" / "deep").mkdir(parents=True)
        (root / "index.htm").write_text("x")
        (root / "public" / "a.html").write_text("x")
        (root / "public" / "deep" / "b.html").write_text("x")

        found = server._scan_html(root)
        assert set(found) == {"index.htm", "public/a.html", "public/deep/b.html"}


class TestMalformedSelectionCannotPoisonTheQueue:
    """A bad selection must be refused at the boundary, not persisted.

    `_el_name` does `el.get("tag")`, so a STRING element raises AttributeError
    when `/queue` summarises the request. Because the selection was already
    written to disk by then, that endpoint keeps returning 500 on every later
    poll until someone deletes the queue file by hand — a one-request,
    self-inflicted, persistent outage from a payload the preview page controls.
    """

    def _submit(self, payload):
        sent: list[tuple[int, dict]] = []

        class _H(server.Handler):
            def __init__(self):
                self.sent = sent

            def _read_body(self):
                return payload

            def _json(self, code, body):
                sent.append((code, body))

        server.Handler._h_submit(_H())
        return sent[0] if sent else (None, None)

    def test_a_string_element_is_refused(self):
        code, body = self._submit(
            {"type": "visual_edit_request", "selection": {"elements": ["x"]}, "comment": "c"}
        )
        assert code == 400
        assert body.get("code") == "selection_malformed"

    def test_a_mixed_list_is_refused(self):
        """One bad element poisons the whole request, so all-or-nothing."""
        code, body = self._submit(
            {
                "type": "visual_edit_request",
                "selection": {"elements": [{"tag": "div"}, "x"]},
                "comment": "c",
            }
        )
        assert code == 400
        assert body.get("code") == "selection_malformed"

    def test_non_list_and_empty_elements_are_refused(self):
        for bad in ({"elements": "div"}, {"elements": {}}, {"elements": []}, {}):
            code, body = self._submit(
                {"type": "visual_edit_request", "selection": bad, "comment": "c"}
            )
            assert code == 400, bad
            assert body.get("code") in ("selection_required", "selection_malformed"), bad

    def test_summarize_tolerates_an_already_poisoned_file(self):
        """The READ path must not 500 on a file written before the guard existed.

        A 500 here is unrecoverable without deleting the queue file by hand, so
        `_summarize_comment` drops non-dict elements instead of crashing.
        """
        summary = server._summarize_comment(
            {
                "cid": "abc",
                "comment": "make it blue",
                "selection": {"elements": ["x", None, 7, {"tag": "div", "id": "hero"}]},
            }
        )
        # The one usable element is found rather than the request being lost.
        assert summary["element"] == "div#hero"
        assert summary["count"] == 1

    def test_summarize_survives_an_all_bad_selection(self):
        summary = server._summarize_comment(
            {"cid": "abc", "comment": "c", "selection": {"elements": ["x", "y"]}}
        )
        assert summary["element"] == ""
        assert summary["count"] == 0


class TestSendIsASingleWinnerCut:
    """The seal is atomic with exactly one winner, and says so in the response.

    The frontend dispatches the agent prompt only when it performed the seal.
    That is only decidable because `/send` reports `already: True` to a loser —
    `ok` is True on BOTH paths, so a client checking only `ok` would dispatch
    twice and the agent would apply every edit in the batch a second time. This
    test locks the contract the frontend guard reads.
    """

    def _send(self, rid="r1"):
        sent: list[tuple[int, dict]] = []

        class _H(server.Handler):
            def __init__(self):
                self.sent = sent

            def _json(self, code, body):
                sent.append((code, body))

        server.Handler._h_send(_H(), {"id": [rid]})
        return sent[-1]

    def test_second_send_reports_already_and_does_not_reseal(self, isolated_queue):
        req = {
            "type": "visual_edit_batch",
            "id": "r1",
            "number": 7,
            "state": "draft",
            "projectId": "p1",
            "comments": [{"cid": "c1", "index": 1, "status": "new", "comment": "make it blue"}],
        }
        (isolated_queue / "r1.json").write_text(json.dumps(req), encoding="utf-8")

        code1, body1 = self._send()
        assert code1 == 200 and body1.get("ok") is True
        assert not body1.get("already"), "the FIRST caller performed the seal"
        first_sent_at = json.loads((isolated_queue / "r1.json").read_text())["sentAt"]

        code2, body2 = self._send()
        assert code2 == 200
        assert body2.get("ok") is True, "ok is True on both paths — hence `already`"
        assert body2.get("already") is True, "the loser must be told it did not seal"
        # The cut happened once: the timestamp is not overwritten by the loser.
        assert json.loads((isolated_queue / "r1.json").read_text())["sentAt"] == first_sent_at


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
    monkeypatch.setattr(server, "_STATIC_SRV", {"srv": None, "url": ""})

    base = server._static_preview_base()
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
        srv = server._STATIC_SRV.get("srv")
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
        srv = server._STATIC_SRV["srv"]
        assert srv.server_address[0] == "127.0.0.1"
        assert str(server._STATIC_SRV["url"]).startswith("http://127.0.0.1:")

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


class _JsonHandler(server.Handler):
    """A `Handler` with no socket, capturing `_json` responses.

    Same trick as `_StubHandler` (never call `BaseHTTPRequestHandler.__init__`,
    which would start serving) but for the JSON API methods rather than the raw
    byte responders. `_cached_body` is what `_read_body` parses.
    """

    def __init__(self, body: bytes = b"", path: str = "/") -> None:
        self.path = path
        self._cached_body = body
        self.json_sent: list[tuple[int, Any]] = []

    def _json(self, code: int, payload) -> None:
        self.json_sent.append((code, payload))


# A canonical AWS example key — `redact_credentials` matches AKIA + 16 chars.
_FAKE_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
_EXFIL_HOST = "evil.example.com"


def _stored(fp) -> dict:
    """Read a queue file back, failing loudly if it became unreadable."""
    req = server._read_request(fp)
    assert req is not None
    return req


@pytest.fixture()
def queued_request(isolated_queue):
    """One real request file in a PRIVATE queue dir.

    `_write_request` re-asserts containment under `DATA_DIR`, so the file cannot
    live in a bare `tmp_path` — but `isolated_queue` redirects `DATA_DIR` itself,
    which satisfies the barrier without touching the shared real queue. Sharing
    it was a cross-worker race: `-n auto --dist loadgroup` distributes these
    tests independently, so two of them ran concurrently against ONE fixed
    filename and the first teardown deleted the file out from under the other.
    """
    rid = "test-thread-redaction"
    fp = server._request_file(server.QUEUE_DIR, rid)
    server._write_request(
        fp,
        {
            "id": rid,
            "number": 1,
            "state": "sent",
            "createdAt": server._now_iso(),
            "comments": [{"cid": "c1", "index": 1, "status": "sent", "comment": "tweak this"}],
        },
    )
    yield rid, fp


class TestThreadTextRedaction:
    """`/thread` text is AGENT output — it must be redacted BEFORE it is stored.

    The note is persisted into the queue JSON and rendered back in the panel, so
    the repo's mandatory output redaction applies. It runs at ingest, not on the
    way out: a leaked credential must not reach disk at all.
    """

    def _post(self, rid, payload, cid=""):
        import json as _json

        h = _JsonHandler(body=_json.dumps(payload).encode("utf-8"))
        qs = {"id": [rid]}
        if cid:
            qs["cid"] = [cid]
        h._h_thread(qs)
        return h

    def test_credential_does_not_survive_into_the_queue_json(self, queued_request):
        rid, fp = queued_request
        h = self._post(rid, {"role": "agent", "text": f"used key {_FAKE_KEY} to deploy"})
        assert h.json_sent[0][0] == 200

        # The assertion that matters: the raw bytes on disk.
        raw = fp.read_text("utf-8")
        assert _FAKE_KEY not in raw
        assert "[REDACTED: credential]" in raw

        stored = _stored(fp)
        note = stored["thread"][-1]
        assert _FAKE_KEY not in note["text"]
        assert note["role"] == "agent"

    def test_credential_in_a_comment_level_note_is_redacted_too(self, queued_request):
        rid, fp = queued_request
        self._post(rid, {"role": "agent", "text": f"token: {_FAKE_KEY}"}, cid="c1")
        assert _FAKE_KEY not in fp.read_text("utf-8")
        stored = _stored(fp)
        note = stored["comments"][0]["thread"][-1]
        assert "[REDACTED: credential]" in note["text"]

    def test_exfiltration_url_is_redacted_host_and_all(self, queued_request):
        """Also pins the ORDER: URLs are redacted before credentials.

        A credential-first pass would rewrite only the `AKIA…` inside the query
        and leave a live, fetchable `https://evil.example.com/collect?key=…`
        standing. So the proof is that the URL was replaced WHOLE by the URL tag
        (which names the host by design), not merely stripped of its token.
        """
        rid, fp = queued_request
        url = f"https://{_EXFIL_HOST}/collect?key={_FAKE_KEY}"
        self._post(rid, {"role": "agent", "text": f"posting to {url}"})
        raw = fp.read_text("utf-8")
        assert _FAKE_KEY not in raw
        assert f"https://{_EXFIL_HOST}" not in raw
        assert f"[REDACTED: suspicious URL to {_EXFIL_HOST}]" in raw
        assert "[REDACTED: credential]" not in raw, "credential pass ran first"

    def test_clean_text_is_stored_verbatim(self, queued_request):
        """Redaction must not mangle ordinary progress notes."""
        rid, fp = queued_request
        msg = "Renamed the button label in src/app/Header.tsx and ran tsc."
        self._post(rid, {"role": "agent", "text": msg})
        assert _stored(fp)["thread"][-1]["text"] == msg

    def test_status_only_note_still_works(self, queued_request):
        """Empty text + a status is a valid call; redaction must not break it."""
        rid, fp = queued_request
        h = self._post(rid, {"role": "agent", "status": "done"}, cid="c1")
        assert h.json_sent[0][0] == 200
        assert _stored(fp)["comments"][0]["status"] == "done"


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

        This previously asserted the opposite — that framing the bare dev server
        was an acceptable degradation "without select-to-edit". It is not: the
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

        It is CLEARED, not handed back. This previously asserted "left exactly as
        today (framed bare)", which is the credential leak: framing it directly
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


@pytest.fixture()
def isolated_queue(tmp_path, monkeypatch):
    """Point the module's data dirs at a private tmp tree.

    Necessary rather than tidy: `_open_draft_file` and `_next_number` scan EVERY
    pending file, so a test that submits into the real queue would both attach to
    the developer's own open drafts and leave files behind. Patched as module
    globals because the containment barrier and both scanners read them at call
    time.
    """
    data = (tmp_path / "data").resolve()
    queue = data / "queue"
    handled = data / "handled"
    queue.mkdir(parents=True)
    handled.mkdir(parents=True)
    monkeypatch.setattr(server, "DATA_DIR", data)
    monkeypatch.setattr(server, "QUEUE_DIR", queue)
    monkeypatch.setattr(server, "HANDLED_DIR", handled)
    # Module state that `_h_submit` reads; pinned so an earlier test cannot leak in.
    monkeypatch.setattr(server, "_ROOT", "")
    monkeypatch.setattr(server, "_TARGET", "")
    monkeypatch.setitem(server._CFG, "projects", [])
    return queue


def _widen_the_race(monkeypatch, delay: float = 0.05):
    """Make the read→write gap wide enough that an unlocked transaction MUST lose.

    Without this the test would depend on thread scheduling; with it, a version
    that takes no lock (or takes it only around the write) fails every run, and a
    version holding it across read-through-write passes every run.
    """
    real_write = server._write_request

    def slow_write(fp, req):
        time.sleep(delay)
        real_write(fp, req)

    monkeypatch.setattr(server, "_write_request", slow_write)


def _submit(project_id: str, text: str) -> _JsonHandler:
    payload = {
        "type": "visual_edit_request",
        "comment": text,
        "projectId": project_id,
        "selection": {"mode": "single", "elements": [{"tag": "div", "id": text}]},
    }
    h = _JsonHandler(body=json.dumps(payload).encode("utf-8"))
    h._h_submit()
    return h


def _run_concurrently(fns) -> None:
    threads = [threading.Thread(target=fn) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a submit thread hung — deadlock?"


class TestConcurrentQueueWrites:
    """`ThreadingHTTPServer` means two dashboard tabs are two handler threads.

    Appending a comment is a read-modify-write of one JSON file. Held only around
    the write (or not at all), both threads read the same draft, append to their
    own copy, and the second write silently discards the other comment — the
    queue file IS the record, so that is data loss, not a stale read.
    """

    def test_concurrent_appends_to_one_draft_keep_both_comments(self, isolated_queue, monkeypatch):
        rid = "1700000000000-aaaaaa"
        server._write_request(
            server._request_file(server.QUEUE_DIR, rid),
            {
                "type": "visual_edit_batch",
                "id": rid,
                "number": 1,
                "state": "draft",
                "projectId": "p-concurrent",
                "projectRoot": "",
                "createdAt": server._now_iso(),
                "sentAt": "",
                "thread": [],
                "comments": [
                    {"cid": "seed", "index": 1, "status": "new", "comment": "seed", "thread": []}
                ],
            },
        )
        _widen_the_race(monkeypatch)

        _run_concurrently(
            [
                lambda: _submit("p-concurrent", "from-tab-a"),
                lambda: _submit("p-concurrent", "from-tab-b"),
            ]
        )

        files = sorted(server.QUEUE_DIR.glob("*.json"))
        assert len(files) == 1, "the project's single open draft was forked"
        req = _stored(files[0])
        got = [c["comment"] for c in req["comments"]]
        assert sorted(got) == ["from-tab-a", "from-tab-b", "seed"], got
        # Sub-numbering stays contiguous — two threads must not both claim index 2.
        assert [c["index"] for c in req["comments"]] == [1, 2, 3]

    def test_concurrent_first_comments_open_exactly_one_draft(self, isolated_queue, monkeypatch):
        """The find-or-create half of the same transaction.

        With the read and the write in separate critical sections, every thread
        sees "no open draft" and creates its own — the project ends up with N
        drafts all numbered 1, which is the same lost-update bug wearing a
        different hat.
        """
        _widen_the_race(monkeypatch)
        labels = [f"c{n}" for n in range(4)]
        _run_concurrently([lambda t=t: _submit("p-fresh", t) for t in labels])

        files = sorted(server.QUEUE_DIR.glob("*.json"))
        assert len(files) == 1, f"opened {len(files)} drafts for one project"
        req = _stored(files[0])
        assert sorted(c["comment"] for c in req["comments"]) == labels
        assert [c["index"] for c in req["comments"]] == [1, 2, 3, 4]

    def test_send_and_submit_do_not_interleave(self, isolated_queue, monkeypatch):
        """Seal-on-send races an in-flight submit.

        Whichever order they land in, the outcome must be self-consistent: either
        the comment made it into the batch before it sealed (all three sent), or
        it opened a fresh draft. What must NOT happen is a comment vanishing, or a
        sealed request carrying a `new` comment the agent will never be told about.
        """
        rid = "1700000000000-bbbbbb"
        server._write_request(
            server._request_file(server.QUEUE_DIR, rid),
            {
                "type": "visual_edit_batch",
                "id": rid,
                "number": 1,
                "state": "draft",
                "projectId": "p-race",
                "projectRoot": "",
                "createdAt": server._now_iso(),
                "sentAt": "",
                "thread": [],
                "comments": [
                    {"cid": "seed", "index": 1, "status": "new", "comment": "seed", "thread": []}
                ],
            },
        )
        _widen_the_race(monkeypatch)

        def send():
            _JsonHandler()._h_send({"id": [rid]})

        _run_concurrently([lambda: _submit("p-race", "late"), send])

        seen = []
        for fp in sorted(server.QUEUE_DIR.glob("*.json")):
            seen.extend(c["comment"] for c in _stored(fp)["comments"])
        assert sorted(seen) == ["late", "seed"], "a comment was lost to the race"
        sealed = _stored(server._request_file(server.QUEUE_DIR, rid))
        if sealed["state"] == "sent":
            assert all(
                c["status"] != "new" for c in sealed["comments"]
            ), "sealed a request with an unsent comment"


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


class TestDeliveryAcknowledgement:
    """`/send` seals; the panel dispatches afterwards. Two steps need an ack.

    A tab closed between the seal and the prompt reaching the agent used to leave
    the request sealed and undeliverable: the send bar only renders for a draft,
    so the batch was stranded with no retry. `deliveredAt` records that the
    dispatch actually happened, which is what lets the panel offer a resend for
    the sealed-but-unacknowledged case.
    """

    def _seal(self, rid):
        h = _JsonHandler()
        h._h_send({"id": [rid]})
        return h.json_sent[0]

    def _ack(self, rid):
        h = _JsonHandler()
        h._h_delivered({"id": [rid]})
        return h.json_sent[0]

    def test_sealed_request_starts_unacknowledged(self, queued_request):
        """The stranded state must be observable, or the panel cannot offer a retry."""
        rid, fp = queued_request
        code, body = self._seal(rid)
        assert code == 200 and body["ok"]
        assert body["request"]["deliveredAt"] == ""
        assert not _stored(fp).get("deliveredAt")

    def test_ack_stamps_delivered_at(self, queued_request):
        rid, fp = queued_request
        self._seal(rid)
        code, body = self._ack(rid)
        assert code == 200 and body["ok"]
        stamped = _stored(fp)["deliveredAt"]
        assert stamped
        assert body["request"]["deliveredAt"] == stamped

    def test_ack_is_idempotent_and_keeps_the_first_timestamp(self, queued_request):
        """A duplicate ack must not make a delivered request look newer."""
        rid, fp = queued_request
        self._seal(rid)
        self._ack(rid)
        first = _stored(fp)["deliveredAt"]
        code, _ = self._ack(rid)
        assert code == 200
        assert _stored(fp)["deliveredAt"] == first

    def test_draft_cannot_be_acknowledged(self, queued_request):
        """Acking a draft would clear the retry bar for the state it exists for."""
        rid, fp = queued_request
        # The fixture writes a sealed request; make it a genuine draft. Both
        # halves are required — `_request_status` treats COMMENT statuses as
        # authoritative, so resetting `state` alone still reads as "sent".
        req = _stored(fp)
        req["state"] = "draft"
        for c in req["comments"]:
            c["status"] = "new"
        server._write_request(fp, req)
        assert server._is_draft(_stored(fp)), "test setup did not produce a draft"

        code, body = self._ack(rid)
        assert code == 409
        assert body["code"] == "not_sealed"
        assert not _stored(fp).get("deliveredAt")

    def test_ack_rejects_a_bad_id_without_touching_disk(self, isolated_queue):
        h = _JsonHandler()
        h._h_delivered({"id": ["../etc/passwd"]})
        code, body = h.json_sent[0]
        assert code == 400 and body["code"] == "id_required"
        assert not list(isolated_queue.glob("*.json"))

    def test_ack_on_a_missing_request_is_404(self, isolated_queue):
        code, body = self._ack("no-such-request")
        assert code == 404 and body["code"] == "not_found"

    def test_delivered_route_is_registered(self):
        """The handler is only reachable if do_POST routes to it."""
        import inspect

        src = inspect.getsource(server.Handler.do_POST)
        assert '"/delivered"' in src
        assert "_h_delivered" in src


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


class TestThreadRedactionOnRead:
    """Redaction is a floor on BOTH edges, not just at ingest.

    `_h_thread` redacts before writing, but ingest is not the only writer: the
    delivery model hands the agent the queue JSON directly, so an entry written
    into the file (or persisted before the ingest pass existed) would otherwise be
    rendered verbatim in the panel.
    """

    def _req_with_thread(self, entries, comment_thread=None):
        return {
            "id": "r1",
            "number": 1,
            "state": "sent",
            "createdAt": server._now_iso(),
            "thread": entries,
            "comments": [
                {
                    "cid": "c1",
                    "index": 1,
                    "status": "sent",
                    "comment": "tweak",
                    "thread": comment_thread if comment_thread is not None else [],
                }
            ],
        }

    def test_request_level_thread_is_redacted_on_serialize(self):
        req = self._req_with_thread([{"role": "agent", "text": f"key {_FAKE_KEY} used"}])
        out = server._summarize(req)
        assert _FAKE_KEY not in json.dumps(out)
        assert "[REDACTED: credential]" in out["thread"][0]["text"]

    def test_comment_level_thread_is_redacted_on_serialize(self):
        req = self._req_with_thread([], comment_thread=[{"role": "agent", "text": _FAKE_KEY}])
        out = server._summarize(req)
        assert _FAKE_KEY not in json.dumps(out)
        assert "[REDACTED: credential]" in out["comments"][0]["thread"][0]["text"]

    def test_url_pass_runs_before_the_credential_pass(self):
        """Same ordering the ingest path pins: a whole exfil URL, host and all.

        Credentials-first would rewrite the token inside the URL and leave the
        host standing, which is the thing that makes the URL dangerous.
        """
        url = f"https://{_EXFIL_HOST}/collect?key={_FAKE_KEY}"
        out = server._summarize(self._req_with_thread([{"role": "agent", "text": f"to {url}"}]))
        text = out["thread"][0]["text"]
        assert _FAKE_KEY not in text
        assert _EXFIL_HOST not in text.replace(f"[REDACTED: suspicious URL to {_EXFIL_HOST}]", "")
        assert f"[REDACTED: suspicious URL to {_EXFIL_HOST}]" in text

    def test_clean_thread_text_is_untouched(self):
        out = server._summarize(
            self._req_with_thread([{"role": "agent", "text": "renamed the CTA"}])
        )
        assert out["thread"][0]["text"] == "renamed the CTA"

    def test_non_text_fields_survive(self):
        """Only `text` is rewritten — role/ts must reach the panel intact."""
        entry = {"role": "agent", "text": "ok", "ts": "2026-01-01T00:00:00Z", "cid": "c1"}
        got = server._summarize(self._req_with_thread([entry]))["thread"][0]
        assert got["role"] == "agent" and got["ts"] == entry["ts"] and got["cid"] == "c1"

    def test_malformed_thread_does_not_break_the_read_path(self):
        """A hand-edited file must not 500 `/queue` for every later poll."""
        req = self._req_with_thread(["not-a-dict", {"role": "agent", "text": "fine"}])
        out = server._summarize(req)
        assert [e["text"] for e in out["thread"]] == ["fine"]

        req2 = self._req_with_thread("not-a-list")
        assert server._summarize(req2)["thread"] == []


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


class TestPreviewUrlProjectResolution:
    """`sourceFile` must survive the removal of the gateway-proxied route.

    Static previews are served from this backend's own loopback origin, so a URL
    parser that only knows the old `/api/proxy/` shape leaves every static
    capture's `sourceFile` empty — the value the visual-edit prompt hands the
    agent as the file to edit.
    """

    def test_static_preview_url_yields_project_and_file(self, monkeypatch):
        monkeypatch.setitem(server._CFG, "projects", [{"id": "p1", "path": "/tmp/x"}])
        monkeypatch.setattr(server, "_static_preview_base", lambda: "http://127.0.0.1:9911/")
        proj, rel = server._proj_for_preview("http://127.0.0.1:9911/p1/src/index.html")
        assert proj is not None and proj["id"] == "p1"
        assert rel == "src/index.html"

    def test_dev_server_route_is_not_read_as_a_file(self, monkeypatch):
        # Same path SHAPE as a static preview; only the origin differs. A route is
        # not a file on disk, so guessing one would misdirect the agent.
        monkeypatch.setitem(server._CFG, "projects", [{"id": "pricing", "path": "/tmp/x"}])
        monkeypatch.setattr(server, "_static_preview_base", lambda: "http://127.0.0.1:9911/")
        proj, rel = server._proj_for_preview("http://localhost:5173/pricing/plans")
        assert proj is None
        assert rel == ""

    def test_legacy_proxy_url_still_parses(self, monkeypatch):
        # Comments captured before the proxied route was deleted still carry it.
        monkeypatch.setitem(server._CFG, "projects", [{"id": "p1", "path": "/tmp/x"}])
        monkeypatch.setattr(server, "_static_preview_base", lambda: "http://127.0.0.1:9911/")
        proj, rel = server._proj_for_preview("/apps/design-tweak/api/proxy/p1/a/b.html")
        assert proj is not None and proj["id"] == "p1"
        assert rel == "a/b.html"
