"""Tests for the dev-server detection and listener discovery region of server.py.

Covers: _lsof_fields, _loopback_listeners, _cwd_for_pids, _serves_html,
_detect_dev_servers, _auto_dev_server, and _valid_target.
"""

from __future__ import annotations

import subprocess
import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest

from kiro_crew.apps.builtins.design_tweak.backend import server

# ---------------------------------------------------------------------------
# _lsof_fields
# ---------------------------------------------------------------------------


class TestLsofFields:
    """Verifies parsing of lsof field-mode output into structured records."""

    def test_returns_empty_when_binary_not_found(self, monkeypatch):
        """If lsof is unavailable, callers must degrade to 'no listeners found'
        rather than raising, because lsof is optional on minimal containers."""
        monkeypatch.setattr(server, "trusted_system_bin", lambda _name: None)
        assert server._lsof_fields(["-nP"]) == []

    def test_parses_pid_and_name_fields(self, monkeypatch):
        """Correct pid-to-name association is critical: a wrong PID means we
        attribute a listener to the wrong process and auto-detect the wrong
        dev server for a project."""
        monkeypatch.setattr(server, "trusted_system_bin", lambda _name: "/usr/bin/lsof")
        fake_output = "p1234\nnlocalhost:3000\np5678\nn127.0.0.1:8080\n"
        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=fake_output, stderr=""
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_result)

        result = server._lsof_fields(["-nP", "-iTCP"])
        assert result == [
            {"pid": "1234", "name": "localhost:3000"},
            {"pid": "5678", "name": "127.0.0.1:8080"},
        ]

    def test_carries_pid_forward_across_multiple_names(self, monkeypatch):
        """A single process may hold multiple sockets; each record must inherit
        the most recent pid line, otherwise ports get misattributed."""
        monkeypatch.setattr(server, "trusted_system_bin", lambda _name: "/usr/bin/lsof")
        # One pid block with two name lines
        fake_output = "p42\nn*:3000\nn*:3001\n"
        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=fake_output, stderr=""
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_result)

        result = server._lsof_fields(["-Fpn"])
        assert len(result) == 2
        assert all(r["pid"] == "42" for r in result)
        assert result[0]["name"] == "*:3000"
        assert result[1]["name"] == "*:3001"

    def test_handles_empty_lines_gracefully(self, monkeypatch):
        """lsof output may contain blank separators between blocks; these must
        not produce malformed records or crashes."""
        monkeypatch.setattr(server, "trusted_system_bin", lambda _name: "/usr/bin/lsof")
        fake_output = "\np100\n\nn*:80\n\n"
        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=fake_output, stderr=""
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_result)

        result = server._lsof_fields(["-Fpn"])
        assert result == [{"pid": "100", "name": "*:80"}]

    @pytest.mark.parametrize("exc", [OSError("perm"), subprocess.TimeoutExpired("lsof", 4)])
    def test_returns_empty_on_subprocess_failure(self, monkeypatch, exc):
        """Subprocess failures (permissions, timeout) must degrade gracefully
        rather than crashing the project-list endpoint."""
        monkeypatch.setattr(server, "trusted_system_bin", lambda _name: "/usr/bin/lsof")

        def raise_exc(*a, **kw):
            raise exc

        monkeypatch.setattr(subprocess, "run", raise_exc)
        assert server._lsof_fields(["-nP"]) == []


# ---------------------------------------------------------------------------
# _loopback_listeners
# ---------------------------------------------------------------------------


class TestLoopbackListeners:
    """Verifies filtering of lsof records to only loopback TCP listeners."""

    @pytest.mark.parametrize(
        "name,expected_port",
        [
            ("127.0.0.1:3000", 3000),
            ("localhost:8080", 8080),
            ("*:5173", 5173),
            ("[::1]:4000", 4000),
            (":9000", 9000),
        ],
        ids=["ipv4-loopback", "localhost", "all-interfaces", "ipv6-loopback", "empty-host"],
    )
    def test_accepts_loopback_variants(self, monkeypatch, name, expected_port):
        """All forms of loopback binding must be recognized; missing any form
        means a real dev server running on that interface goes undetected."""
        records = [{"pid": "10", "name": name}]
        monkeypatch.setattr(server, "_lsof_fields", lambda _args: records)

        result = server._loopback_listeners()
        assert result == {expected_port: 10}

    @pytest.mark.parametrize(
        "name",
        [
            "192.168.1.5:3000",
            "10.0.0.1:8080",
            "example.com:443",
        ],
        ids=["private-ipv4", "private-10-net", "external-hostname"],
    )
    def test_rejects_non_loopback(self, monkeypatch, name):
        """Non-loopback listeners must be excluded; including them would let
        auto-detect point the preview at a remote host."""
        records = [{"pid": "10", "name": name}]
        monkeypatch.setattr(server, "_lsof_fields", lambda _args: records)

        result = server._loopback_listeners()
        assert result == {}

    def test_skips_entries_without_colon(self, monkeypatch):
        """A name field like 'pipe' or '/dev/null' has no port separator and
        must not crash the parser."""
        records = [{"pid": "1", "name": "/dev/null"}]
        monkeypatch.setattr(server, "_lsof_fields", lambda _args: records)
        assert server._loopback_listeners() == {}

    def test_skips_non_numeric_port(self, monkeypatch):
        """A name like 'localhost:abc' must be silently dropped; raising would
        crash the project-list endpoint."""
        records = [{"pid": "1", "name": "localhost:abc"}]
        monkeypatch.setattr(server, "_lsof_fields", lambda _args: records)
        assert server._loopback_listeners() == {}


# ---------------------------------------------------------------------------
# _cwd_for_pids
# ---------------------------------------------------------------------------


class TestCwdForPids:
    """Verifies process-working-directory resolution via lsof."""

    def test_empty_pids_returns_empty(self, monkeypatch):
        """An empty input must short-circuit without calling lsof, avoiding
        a pointless subprocess spawn on every poll when no listeners exist."""
        calls = []
        monkeypatch.setattr(server, "_lsof_fields", lambda args: (calls.append(args), [])[1])
        result = server._cwd_for_pids([])
        assert result == {}
        assert calls == []

    def test_maps_pid_to_cwd(self, monkeypatch):
        """Correct pid-to-cwd mapping is the foundation of project matching;
        a wrong mapping attributes a listener to the wrong project."""
        records = [
            {"pid": "100", "name": "/home/user/project-a"},
            {"pid": "200", "name": "/home/user/project-b"},
        ]
        monkeypatch.setattr(server, "_lsof_fields", lambda _args: records)

        result = server._cwd_for_pids([100, 200])
        assert result == {100: "/home/user/project-a", 200: "/home/user/project-b"}

    def test_deduplicates_pids_in_arg(self, monkeypatch):
        """Multiple ports held by the same PID should produce one lsof query
        for that PID, not a repeated entry that can confuse lsof -p."""
        captured_args = []

        def fake_lsof(args):
            captured_args.append(args)
            return [{"pid": "100", "name": "/tmp"}]

        monkeypatch.setattr(server, "_lsof_fields", fake_lsof)
        server._cwd_for_pids([100, 100, 100])
        # The -p argument should contain the pid only once
        p_arg = captured_args[0][captured_args[0].index("-p") + 1]
        assert p_arg == "100"

    def test_skips_non_numeric_pid(self, monkeypatch):
        """If lsof returns a malformed pid field (e.g. 'p' prefix residue), the
        record must be skipped rather than crash the entire cwd resolution."""
        records = [
            {"pid": "notanumber", "name": "/home/user/project"},
            {"pid": "200", "name": "/home/user/other"},
        ]
        monkeypatch.setattr(server, "_lsof_fields", lambda _args: records)

        result = server._cwd_for_pids([200])
        # The non-numeric pid is silently dropped; the valid one is kept
        assert result == {200: "/home/user/other"}


# ---------------------------------------------------------------------------
# _serves_html
# ---------------------------------------------------------------------------


class TestServesHtml:
    """Verifies the HTML content-type probe that distinguishes dev servers
    from API servers and language servers sharing the same project folder."""

    def test_true_on_html_content_type(self, monkeypatch):
        """A dev server serves HTML pages; failing to detect this means the
        auto-detect never picks it as the preview target."""
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: mock_resp)

        assert server._serves_html(3000) is True

    def test_false_on_json_content_type(self, monkeypatch):
        """An API server responds with JSON; treating it as a dev server would
        show raw JSON in the preview iframe instead of the user's app."""
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: mock_resp)

        assert server._serves_html(3000) is False

    def test_true_on_http_error_with_html(self, monkeypatch):
        """A dev server can 404 on '/' but still serve HTML (e.g. a custom error
        page); we must detect the content type even on error responses."""
        exc = urllib.error.HTTPError(
            "http://127.0.0.1:3000/", 404, "Not Found", {"Content-Type": "text/html"}, None
        )

        def raise_http_error(*a, **kw):
            raise exc

        monkeypatch.setattr(urllib.request, "urlopen", raise_http_error)
        assert server._serves_html(3000) is True

    def test_false_on_http_error_without_html(self, monkeypatch):
        """A non-HTML error response (e.g. plain text 500) is not a dev server."""
        exc = urllib.error.HTTPError(
            "http://127.0.0.1:3000/", 500, "Error", {"Content-Type": "text/plain"}, None
        )

        def raise_http_error(*a, **kw):
            raise exc

        monkeypatch.setattr(urllib.request, "urlopen", raise_http_error)
        assert server._serves_html(3000) is False

    @pytest.mark.parametrize(
        "exc",
        [
            urllib.error.URLError("refused"),
            OSError("network unreachable"),
            ValueError("bad url"),
        ],
        ids=["url-error", "os-error", "value-error"],
    )
    def test_false_on_connection_failure(self, monkeypatch, exc):
        """When the port is unreachable, the function must return False (not
        raise), so callers can continue probing other candidates."""

        def raise_exc(*a, **kw):
            raise exc

        monkeypatch.setattr(urllib.request, "urlopen", raise_exc)
        assert server._serves_html(9999) is False


# ---------------------------------------------------------------------------
# _detect_dev_servers
# ---------------------------------------------------------------------------


class TestDetectDevServers:
    """Verifies the dev-server discovery pipeline that matches listeners to
    projects by working directory containment."""

    def test_matches_listener_whose_cwd_is_project_root(self, monkeypatch, tmp_path):
        """The most common case: `npm run dev` started from the project root.
        Failing to detect this means the user must manually paste the URL."""
        monkeypatch.setattr(server, "_loopback_listeners", lambda: {3000: 42})
        monkeypatch.setattr(server, "_cwd_for_pids", lambda _pids: {42: str(tmp_path)})
        monkeypatch.setattr(server, "_serves_html", lambda _port: True)

        result = server._detect_dev_servers(tmp_path)
        assert len(result) == 1
        assert result[0]["port"] == 3000
        assert result[0]["pid"] == 42
        assert result[0]["depth"] == 0
        assert result[0]["servesHtml"] is True

    def test_matches_nested_cwd(self, monkeypatch, tmp_path):
        """Monorepo servers often start from a subdirectory (apps/web/); the
        depth-based sorting must still pick them up as valid candidates."""
        nested = tmp_path / "packages" / "app"
        nested.mkdir(parents=True)
        monkeypatch.setattr(server, "_loopback_listeners", lambda: {5173: 99})
        monkeypatch.setattr(server, "_cwd_for_pids", lambda _pids: {99: str(nested)})
        monkeypatch.setattr(server, "_serves_html", lambda _port: True)

        result = server._detect_dev_servers(tmp_path)
        assert len(result) == 1
        assert result[0]["depth"] == 2

    def test_excludes_unrelated_cwd(self, monkeypatch, tmp_path):
        """A listener whose cwd is outside the project must never appear as a
        candidate; otherwise the preview attaches to an unrelated server."""
        monkeypatch.setattr(server, "_loopback_listeners", lambda: {8080: 50})
        monkeypatch.setattr(server, "_cwd_for_pids", lambda _pids: {50: "/other/project"})
        monkeypatch.setattr(server, "_serves_html", lambda _port: True)

        result = server._detect_dev_servers(tmp_path)
        assert result == []

    def test_excludes_listener_without_cwd(self, monkeypatch, tmp_path):
        """If lsof cannot resolve a PID's cwd (race: process died), the listener
        must be skipped rather than crash."""
        monkeypatch.setattr(server, "_loopback_listeners", lambda: {3000: 42})
        monkeypatch.setattr(server, "_cwd_for_pids", lambda _pids: {})
        monkeypatch.setattr(server, "_serves_html", lambda _port: True)

        result = server._detect_dev_servers(tmp_path)
        assert result == []

    def test_sorts_html_first_then_by_depth(self, monkeypatch, tmp_path):
        """Sorting ensures the best candidate is first: HTML-serving beats
        non-HTML, and shallower cwd beats deeper. Wrong order means the auto-
        detect picks a language server over the real dev server."""
        nested = tmp_path / "packages" / "web"
        nested.mkdir(parents=True)
        monkeypatch.setattr(
            server, "_loopback_listeners", lambda: {3000: 10, 3001: 20, 3002: 30}
        )
        monkeypatch.setattr(
            server, "_cwd_for_pids",
            lambda _pids: {10: str(nested), 20: str(tmp_path), 30: str(tmp_path)},
        )
        # 3000 (depth=2, html), 3001 (depth=0, no html), 3002 (depth=0, html)
        html_map = {3000: True, 3001: False, 3002: True}
        monkeypatch.setattr(server, "_serves_html", lambda port: html_map[port])

        result = server._detect_dev_servers(tmp_path)
        # HTML-serving first (3002 depth=0, 3000 depth=2), then non-html (3001)
        assert [r["port"] for r in result] == [3002, 3000, 3001]

    def test_skips_probe_when_probe_false(self, monkeypatch, tmp_path):
        """probe=False suppresses the HTTP request; servesHtml must be None so
        callers know no probe was made (not confused with False=not-html)."""
        monkeypatch.setattr(server, "_loopback_listeners", lambda: {3000: 42})
        monkeypatch.setattr(server, "_cwd_for_pids", lambda _pids: {42: str(tmp_path)})
        # _serves_html should NOT be called
        monkeypatch.setattr(server, "_serves_html", lambda _port: (_ for _ in ()).throw(
            AssertionError("_serves_html should not be called")
        ))

        result = server._detect_dev_servers(tmp_path, probe=False)
        assert len(result) == 1
        assert result[0]["servesHtml"] is None


# ---------------------------------------------------------------------------
# _auto_dev_server
# ---------------------------------------------------------------------------


class TestAutoDevServer:
    """Verifies the single-result auto-detect that powers the automatic
    dev-server attachment on the /projects endpoint."""

    def test_returns_url_when_exactly_one_html_candidate(self, monkeypatch, tmp_path):
        """When one candidate clearly serves HTML, auto-detect must return its
        URL; failing to do so forces the user to paste it manually."""
        monkeypatch.setattr(
            server, "_detect_dev_servers",
            lambda root: [{"port": 5173, "servesHtml": True, "url": "http://localhost:5173"}],
        )
        assert server._auto_dev_server(tmp_path) == "http://localhost:5173"

    def test_returns_empty_when_no_candidates(self, monkeypatch, tmp_path):
        """No candidates means nothing to attach; returning a URL would point
        the preview at a non-existent server."""
        monkeypatch.setattr(server, "_detect_dev_servers", lambda root: [])
        assert server._auto_dev_server(tmp_path) == ""

    def test_returns_empty_when_multiple_html_candidates(self, monkeypatch, tmp_path):
        """Ambiguity (two+ HTML servers) must NOT guess; guessing wrong silently
        shows the wrong app in the preview iframe."""
        monkeypatch.setattr(
            server, "_detect_dev_servers",
            lambda root: [
                {"port": 3000, "servesHtml": True, "url": "http://localhost:3000"},
                {"port": 5173, "servesHtml": True, "url": "http://localhost:5173"},
            ],
        )
        assert server._auto_dev_server(tmp_path) == ""

    def test_returns_empty_when_only_non_html(self, monkeypatch, tmp_path):
        """Candidates that don't serve HTML are API servers or language servers;
        auto-detect must not select them."""
        monkeypatch.setattr(
            server, "_detect_dev_servers",
            lambda root: [{"port": 4000, "servesHtml": False, "url": "http://localhost:4000"}],
        )
        assert server._auto_dev_server(tmp_path) == ""


# ---------------------------------------------------------------------------
# _valid_target (the SSRF guard for dev-server URLs)
# ---------------------------------------------------------------------------


class TestValidTarget:
    """Verifies the URL allowlist that prevents the preview iframe from being
    pointed at arbitrary hosts (SSRF via the project settings endpoint)."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:3000",
            "http://127.0.0.1:8080",
            "http://localhost",
            "http://127.0.0.1",
            "http://localhost:1",
            "http://localhost:65535",
        ],
        ids=["localhost-port", "ipv4-port", "localhost-no-port", "ipv4-no-port",
             "min-port", "max-port"],
    )
    def test_accepts_valid_loopback_urls(self, url):
        """All valid loopback+port combinations must pass; rejecting any blocks
        a legitimate dev server URL from being configured."""
        assert server._valid_target(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://localhost:3000",
            "http://evil.com:3000",
            "http://192.168.1.1:3000",
            "http://10.0.0.1:8080",
            "ftp://localhost:21",
            "http://localhost:notaport",
            "http://localhost:99999",
            "http://localhost:0",
        ],
        ids=["https-scheme", "external-host", "private-ipv4", "class-a-private",
             "ftp-scheme", "non-numeric-port", "port-overflow", "port-zero"],
    )
    def test_rejects_invalid_urls(self, url):
        """Each rejection prevents a distinct SSRF vector: wrong scheme bypasses
        the loopback check, wrong host is direct SSRF, bad ports crash later."""
        assert server._valid_target(url) is False

    def test_rejects_empty_string(self):
        """An empty URL from a cleared form field must not pass the barrier."""
        assert server._valid_target("") is False

    def test_rejects_malformed_url(self):
        """Completely broken input must not raise; it should return False so the
        endpoint can report a validation error rather than a 500."""
        assert server._valid_target("://not-a-url") is False
