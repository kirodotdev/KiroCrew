"""The ``kirocrew-computer`` shim's session identity and its loopback ``_invoke`` wire.

An unresolved session key travels as an ``unresolved:<pid>#<nonce>`` placeholder that
keeps snapshot slots apart and never becomes an identity, and the real ``_invoke`` HTTP
leg, run against loopback listeners, never hands the internal secret to a proxy and
never declares a key it could not resolve.

One topic of the ``test_mcp_computer*.py`` family; the fixtures and dispatch helpers
the family shares come from ``mcp_computer_helpers.py``.
"""

from __future__ import annotations

import http.server
import inspect
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest
from mcp_computer_helpers import home_fixture  # noqa: F401
from mcp_computer_helpers import keystone_fixture  # noqa: F401
from mcp_computer_helpers import _enable

from kiro_crew import mcp_computer, mcp_core
from kiro_crew.computer_use.types import ERROR_PREFIX, TOOL_LIST_APPS, AppRef
from kiro_crew.mcp_caller import CallerContext, set_current_caller, set_current_tenant_nonce


class TestUnresolvedSessionsAreNamespaced:
    """GPT 5.6 BLOCKING, and the aliasing half of it was real.

    ``SnapshotIndex`` namespaces entries by ``(session_key, window_key)``. With an
    EMPTY key, every unresolved session shared the single ``("", window)`` slot — and
    on macOS unresolved is the normal case, since neither accepted identity source
    exists for a GUI-launched kiro-cli. Two concurrent sessions observing the same
    window therefore overwrote each other's element indices, and each one's own
    fingerprint check still passed, because both trees describe the same window. That
    is a wrong-target action with nothing reporting it.

    **GPT's prescribed fix was to refuse an empty key. Not taken** — that is exactly
    the refusal removed by product decision, and it made the feature unusable on its
    only supported platform. The aliasing is fixed by NAMESPACING instead: kiro-cli
    spawns one shim process per session, so the shim's own pid separates the
    namespaces precisely as far as the sessions are genuinely separate, and nothing is
    refused. The security posture is unchanged; only the cache key is.

    The POOLED half of the same aliasing: "One shim process per session" is
    the 1:1 topology's premise, and a pooled backend breaks it: one process serves N
    connections, so the pid separates nothing and every unnamed co-tenant collapsed
    back onto a single ``unresolved:<pid>`` key. The pid is now joined by the
    gateway-minted per-CONNECTION nonce, which separates them again — still a
    separator, still not attribution, and still nothing refused.
    """

    @pytest.fixture(autouse=True)
    def _no_ambient_nonce(self):
        """No injected nonce unless a test installs one.

        The nonce lives in a ``ContextVar`` the dispatch loop sets and clears, so a
        test leaving one installed would silently rewrite every later test's
        expected key.
        """
        set_current_tenant_nonce("")
        yield
        set_current_tenant_nonce("")

    def test_two_unresolved_sessions_do_not_share_a_snapshot_slot(self):
        """The bug, at the layer it actually lived in."""
        from kiro_crew.computer_use.index import SnapshotIndex
        from kiro_crew.computer_use.types import ElementRec, Snapshot

        app = AppRef(name="Notes", pid=1, window_id=7)

        def snap(title: str) -> Snapshot:
            return Snapshot(
                app=app,
                elements=(ElementRec(index=0, role="AXButton", title=title),),
                captured_at=100.0,
            )

        # The old behaviour: one shared slot, so B's tree answers A's lookup.
        aliased = SnapshotIndex()
        aliased.put(snap("A"), session_key="")
        aliased.put(snap("B"), session_key="")
        assert aliased.get(app.window_key, session_key="", now=100.0).elements[0].title == "B"

        # The fix: distinct per-process keys keep each session's own indices.
        namespaced = SnapshotIndex()
        namespaced.put(snap("A"), session_key="unresolved:111")
        namespaced.put(snap("B"), session_key="unresolved:222")
        for key, expected in (("unresolved:111", "A"), ("unresolved:222", "B")):
            got = namespaced.get(app.window_key, session_key=key, now=100.0)
            assert got is not None and got.elements[0].title == expected, key

    def test_the_placeholder_is_per_process(self):
        key = mcp_computer._unresolved_session_key()
        assert key == f"{mcp_computer.UNRESOLVED_SESSION_PREFIX}{os.getpid()}"

    def test_two_unnamed_co_tenants_of_ONE_process_do_not_share_a_slot(self, monkeypatch):
        """The pooled aliasing at the layer it lives in — one pid, two connections.

        Both co-tenants run in the SAME shim process, so ``os.getpid()`` is pinned to
        one value here deliberately: that is the whole premise the pooled topology
        breaks. Without the nonce half both keys are the same string and the second
        snapshot answers the first session's lookup — the wrong-target action the
        fingerprint check cannot catch, because both trees describe the same window.
        """
        from kiro_crew.computer_use.index import SnapshotIndex
        from kiro_crew.computer_use.types import ElementRec, Snapshot

        monkeypatch.setattr(os, "getpid", lambda: 5150)
        app = AppRef(name="Notes", pid=1, window_id=7)

        def snap(title: str) -> Snapshot:
            return Snapshot(
                app=app,
                elements=(ElementRec(index=0, role="AXButton", title=title),),
                captured_at=100.0,
            )

        keys: list[str] = []
        for nonce in ("aaaa1111", "bbbb2222"):
            set_current_tenant_nonce(nonce)
            keys.append(mcp_computer._unresolved_session_key())

        assert keys[0] != keys[1], "one pid, two connections: the keys must differ"

        index = SnapshotIndex()
        index.put(snap("A"), session_key=keys[0])
        index.put(snap("B"), session_key=keys[1])
        for key, expected in zip(keys, ("A", "B")):
            got = index.get(app.window_key, session_key=key, now=100.0)
            assert got is not None and got.elements[0].title == expected, key

    def test_the_nonce_half_is_read_at_CALL_time_too(self, monkeypatch):
        """One process serves many connections in sequence as well as concurrently.

        A nonce captured once per process would namespace by process again, which is
        the bug. Each call reads the connection the call arrived on.
        """
        monkeypatch.setattr(os, "getpid", lambda: 5150)
        set_current_tenant_nonce("first")
        assert mcp_computer._unresolved_session_key() == "unresolved:5150#first"
        set_current_tenant_nonce("second")
        assert mcp_computer._unresolved_session_key() == "unresolved:5150#second"

    def test_the_nonce_never_becomes_an_IDENTITY(self, monkeypatch):
        """A separator must not be promoted to attribution.

        The nonce names a connection, not a principal. If the strict resolver ever
        read it, an unnamed caller would arrive at every consumer of a session key —
        cron ownership, callback routing, audit — carrying a key that names nobody
        while LOOKING resolved. So with a nonce installed and no identity source,
        strict resolution must still come back empty, and the shim must still take
        the unresolved path.
        """
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
        set_current_caller(None)
        set_current_tenant_nonce("cafebabe")

        assert mcp_core._resolve_session_key_strict() == ""
        assert mcp_computer._unresolved_session_key().startswith(
            mcp_computer.UNRESOLVED_SESSION_PREFIX
        )

    def test_a_named_co_tenant_is_unaffected_by_the_nonce(self, monkeypatch):
        """The nonce is the fallback's business only.

        A caller the gateway CAN name already has a per-session key; appending a
        connection nonce to it would split one session's own namespace across a
        reconnect for no reason.
        """
        set_current_caller(CallerContext(session_key="dashboard:main"))
        set_current_tenant_nonce("cafebabe")
        try:
            assert mcp_core._resolve_session_key_strict() == "dashboard:main"
        finally:
            set_current_caller(None)

    def test_the_key_is_never_a_header_claim(self):
        """The placeholder goes in the BODY only; ``X-Session-Key`` is a claim it is not.

        ``_declares_identity`` is the shim's own routing: a strictly-resolved key is
        declared and kernel-verified, the placeholder (with or without the nonce
        half) is not declared at all. See ``TestUnresolvedKeysAreNeverDeclared``.
        """
        set_current_tenant_nonce("cafebabe")
        assert not mcp_computer._declares_identity(mcp_computer._unresolved_session_key())
        set_current_tenant_nonce("")
        assert not mcp_computer._declares_identity(mcp_computer._unresolved_session_key())
        assert mcp_computer._declares_identity("dashboard:main")

    def test_it_is_read_at_CALL_time_not_captured_at_import(self, monkeypatch):
        """A ``fork``ed child must not inherit the parent's string.

        Capturing the pid at import would make every forked shim re-alias with its
        parent — reintroducing the exact bug one level down, and in the shape hardest
        to notice.
        """
        monkeypatch.setattr(os, "getpid", lambda: 4242)
        assert mcp_computer._unresolved_session_key().endswith("4242")

    def test_the_placeholder_is_not_presented_as_a_real_identity(self):
        """It is a namespace separator, not attribution.

        An audit reader must not mistake a pid for a session identity, so the prefix
        names it — a bare number would read like a resolved key.
        """
        assert mcp_computer._unresolved_session_key().startswith("unresolved:")

    def test_a_RESOLVED_key_is_never_replaced(self, keystone: Path, monkeypatch):
        """The fallback must not shadow a genuine identity, which would DESTROY the
        attribution the strict resolver exists to provide."""
        _enable(keystone)
        posted: list[Any] = []
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:main")
        monkeypatch.setattr(
            mcp_computer, "_invoke", lambda *a, **k: posted.append(a) or {"text": "ok"}
        )
        mcp_computer._call_tool_inner(TOOL_LIST_APPS, {})
        assert posted and posted[0][0] == "dashboard:main"


class TestTheInvokeCallIsNeverProxied:
    """``_invoke``'s HTTP body, exercised for real against two live listeners.

    Every other test in this file patches ``mcp_computer._invoke`` wholesale, so
    the request-building and opener code inside it had NO coverage — the leak this
    guards could have been reintroduced without a single failure. That matters
    more here than at a typical loopback site: this route's request carries
    ``X-Internal-Secret``, and the gateway handler behind it is the authoritative
    fail-CLOSED computer-use gate, so a proxy that could answer it is a proxy that
    could authorise reading a password field.

    Real sockets on port 0 following ``test_cron_trigger.py``: the kernel hands
    out free ports, so no ``xdist_group`` marker is needed. ``_API`` and
    ``_internal_secret`` are patched on ``mcp_computer``'s own namespace (it
    imports both FROM ``mcp_core``, binding local names), which keeps the real
    secret file out of the test entirely.
    """

    CANARY = "canary-not-a-real-secret"
    PROXY_ENV_KEYS = (
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
        "no_proxy",
        "NO_PROXY",
        # getproxies_environment ignores uppercase HTTP_PROXY when REQUEST_METHOD
        # is set (the httpoxy CGI guard), which would silently void this test.
        "REQUEST_METHOD",
    )

    @staticmethod
    def _serve(sink: list[dict]):
        """A listener that records the secret it saw and replies with shim-valid JSON."""

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                sink.append(
                    {
                        "requestline": self.requestline,
                        "secret": self.headers.get("X-Internal-Secret"),
                        "session_key": self.headers.get("X-Session-Key"),
                        "body": json.loads(raw),
                    }
                )
                payload = b'{"text": "listener-answered"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt, *args):
                pass  # keep pytest output clean

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, server.server_address[1]

    def test_the_secret_reaches_the_gateway_and_not_the_proxy(self, monkeypatch):
        """``HTTP_PROXY`` set, ``no_proxy`` unset: the shape that actually leaks."""
        gateway_hits: list[dict] = []
        proxy_hits: list[dict] = []
        gateway, gateway_port = self._serve(gateway_hits)
        proxy, proxy_port = self._serve(proxy_hits)
        try:
            for key in self.PROXY_ENV_KEYS:
                monkeypatch.delenv(key, raising=False)
            monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy_port}")
            # Paired resolution: an attempt threads (base, socket_path).
            # The empty socket keeps this case on TCP, which is what the proxy
            # question is about.
            monkeypatch.setattr(
                mcp_computer,
                "_resolve_api_target",
                lambda: (f"http://127.0.0.1:{gateway_port}", ""),
            )
            monkeypatch.setattr(mcp_computer, "_internal_secret", lambda: self.CANARY)

            decoded = mcp_computer._invoke("dashboard:main", TOOL_LIST_APPS, {})
        finally:
            # flake-ok: shutdown() of a port-0 loopback listener returns at serve_forever's next 0.5 s poll; moved verbatim, tracked for a de-flake pass
            gateway.shutdown()
            proxy.shutdown()

        assert proxy_hits == [], f"the internal secret reached the proxy: {proxy_hits}"
        assert [h["secret"] for h in gateway_hits] == [self.CANARY]
        # Relative request line confirms a direct connection rather than the
        # absolute form urllib emits when it treats the host as proxied.
        assert gateway_hits[0]["requestline"] == f"POST {mcp_computer.INVOKE_PATH} HTTP/1.1"
        # The round trip completed through the new opener, so the migration did not
        # trade the leak for a silently-swallowed transport error — ``_invoke``
        # returns ``{"error": ...}`` for any failure rather than raising.
        assert decoded == {"text": "listener-answered"}
        assert gateway_hits[0]["body"]["session_key"] == "dashboard:main"

    def test_no_proxy_naming_localhost_only_still_does_not_expose_it(self, monkeypatch):
        """``no_proxy=localhost`` is the common corporate default and looks like
        coverage, but it matches the host STRING — ``_API`` spelled either way must
        not depend on it."""
        gateway_hits: list[dict] = []
        proxy_hits: list[dict] = []
        gateway, gateway_port = self._serve(gateway_hits)
        proxy, proxy_port = self._serve(proxy_hits)
        try:
            for key in self.PROXY_ENV_KEYS:
                monkeypatch.delenv(key, raising=False)
            monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy_port}")
            monkeypatch.setenv("no_proxy", "localhost")
            # Paired resolution: an attempt threads (base, socket_path).
            # The empty socket keeps this case on TCP, which is what the proxy
            # question is about.
            monkeypatch.setattr(
                mcp_computer,
                "_resolve_api_target",
                lambda: (f"http://127.0.0.1:{gateway_port}", ""),
            )
            monkeypatch.setattr(mcp_computer, "_internal_secret", lambda: self.CANARY)

            mcp_computer._invoke("dashboard:main", TOOL_LIST_APPS, {})
        finally:
            # flake-ok: shutdown() of a port-0 loopback listener returns at serve_forever's next 0.5 s poll; moved verbatim, tracked for a de-flake pass
            gateway.shutdown()
            proxy.shutdown()

        assert proxy_hits == [], f"the secret proxied despite no_proxy=localhost: {proxy_hits}"
        assert [h["secret"] for h in gateway_hits] == [self.CANARY]


class TestUnresolvedKeysAreNeverDeclared:
    """The ``unresolved:`` placeholder rides in the body, never in ``X-Session-Key``.

    The header is an identity CLAIM. On the AF_UNIX leg ``token_auth._verify_unix_peer``
    kernel-resolves the peer pid's ``session_pid_<pid>`` ancestry and denies
    ``403 peer_session_mismatch`` when the recovered key differs from the declared one.
    A placeholder names no session, so it can never match, and declaring it makes every
    dashboard-launched Computer Use call on macOS fail with a bare ``Error: Forbidden``
    while ``kirocrew computer apps`` (which never takes that leg) works. The
    empty-header arm of that gate is "nothing session-scoped is claimed, nothing to
    verify", which is the truth of an unnamed shim.

    Asserted at the WIRE, on a real listener, so a refactor that rebuilds the header
    dict cannot pass by accident.
    """

    @pytest.fixture(autouse=True)
    def _no_ambient_nonce(self):
        set_current_tenant_nonce("")
        yield
        set_current_tenant_nonce("")

    @staticmethod
    def _serve(sink: list[dict]):
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                sink.append(
                    {
                        "has_session_header": "X-Session-Key" in self.headers,
                        "session_header": self.headers.get("X-Session-Key"),
                        "has_token_header": "X-Session-Token" in self.headers,
                        "token_header": self.headers.get("X-Session-Token"),
                        "body": json.loads(raw),
                    }
                )
                payload = b'{"text": "ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, server.server_address[1]

    def _invoke_through(self, monkeypatch, session_key: str) -> dict:
        hits: list[dict] = []
        server, port = self._serve(hits)
        try:
            monkeypatch.setattr(
                mcp_computer, "_resolve_api_target", lambda: (f"http://127.0.0.1:{port}", "")
            )
            monkeypatch.setattr(mcp_computer, "_internal_secret", lambda: "not-a-secret")
            decoded = mcp_computer._invoke(session_key, TOOL_LIST_APPS, {})
        finally:
            # flake-ok: shutdown() of a port-0 loopback listener returns at serve_forever's next 0.5 s poll; moved verbatim, tracked for a de-flake pass
            server.shutdown()
        assert decoded == {"text": "ok"}
        assert len(hits) == 1
        return hits[0]

    def test_an_unresolved_key_sends_no_header_and_keeps_the_body_namespace(self, monkeypatch):
        key = mcp_computer._unresolved_session_key()
        hit = self._invoke_through(monkeypatch, key)
        assert hit["has_session_header"] is False, hit
        # The body placeholder still scopes ``SnapshotIndex`` and the audit line.
        assert hit["body"]["session_key"] == key
        assert key.startswith(mcp_computer.UNRESOLVED_SESSION_PREFIX)

    def test_a_pooled_unresolved_key_with_a_nonce_sends_no_header_either(self, monkeypatch):
        set_current_tenant_nonce("cafebabe")
        key = mcp_computer._unresolved_session_key()
        assert mcp_computer.UNRESOLVED_TENANT_SEPARATOR in key
        hit = self._invoke_through(monkeypatch, key)
        assert hit["has_session_header"] is False, hit
        assert hit["body"]["session_key"] == key

    def test_a_strictly_resolved_key_is_still_declared(self, monkeypatch):
        """The gate still has a claim to kernel-verify when there is a real one."""
        hit = self._invoke_through(monkeypatch, "dashboard:main")
        assert hit["session_header"] == "dashboard:main"
        assert hit["body"]["session_key"] == "dashboard:main"

    def test_a_declared_key_carries_its_session_token(self, monkeypatch):
        """HEADLINE: the token travels WITH the key, at the wire.

        On a pid hosting several sessions the peer check cannot tell which of them
        holds the socket from kernel credentials, so it requires a token naming
        exactly the declared key and refuses the declaration otherwise. Without
        this header every computer-use call on a shared runtime is refused -- the
        founder's included, which worked before that demand existed. Asserted on a
        real listener so a refactor that rebuilds the header dict cannot drop it
        silently, which is how it was missing in the first place.
        """
        monkeypatch.setattr(
            mcp_computer, "_session_token_header", lambda: {"X-Session-Token": "a-signed-token"}
        )
        hit = self._invoke_through(monkeypatch, "dashboard:main")
        assert hit["session_header"] == "dashboard:main"
        assert hit["token_header"] == "a-signed-token"

    def test_an_undeclared_key_sends_no_token_either(self, monkeypatch):
        """The converse: the token is an attestation OF a declaration, so it must
        not travel alone. Sending one where no key is claimed would offer the
        gateway an identity the caller deliberately withheld."""
        monkeypatch.setattr(
            mcp_computer, "_session_token_header", lambda: {"X-Session-Token": "a-signed-token"}
        )
        hit = self._invoke_through(monkeypatch, mcp_computer._unresolved_session_key())
        assert hit["has_session_header"] is False, hit
        assert hit["has_token_header"] is False, hit

    def test_the_call_path_routes_an_unresolved_identity_without_a_header(
        self, keystone: Path, monkeypatch
    ):
        """End to end from ``_call_tool_inner``: strict resolver empty -> no header."""
        _enable(keystone)
        seen: list[tuple[str, dict]] = []

        def fake_invoke(session_key, name, args):
            seen.append((session_key, {"declared": mcp_computer._declares_identity(session_key)}))
            return {"text": "ok"}

        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
        monkeypatch.setattr(mcp_computer, "_invoke", fake_invoke)
        assert mcp_computer._call_tool_inner(TOOL_LIST_APPS, {}) == "ok"
        assert seen and seen[0][0].startswith(mcp_computer.UNRESOLVED_SESSION_PREFIX)
        assert seen[0][1]["declared"] is False

    def test_the_header_preflight_still_guards_a_resolved_key(self, keystone: Path, monkeypatch):
        """A resolved key that cannot be a header is still refused before the wire."""
        _enable(keystone)
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:tab\u2014x")
        monkeypatch.setattr(
            mcp_computer, "_invoke", lambda *a, **k: pytest.fail("must not reach the wire")
        )
        result = mcp_computer._call_tool_inner(TOOL_LIST_APPS, {})
        assert result.startswith(ERROR_PREFIX)
        assert "invalid in HTTP headers" in result

    def test_the_header_is_gated_by_the_prefix_not_by_a_caller_flag(self):
        """Source-level guard: ``_invoke`` must consult ``_declares_identity`` itself.

        A flag threaded from the caller is the shape that lets a future call site
        put the placeholder back in the header by forgetting to pass it.
        """
        src = inspect.getsource(mcp_computer._invoke)
        assert "_declares_identity(session_key)" in src
        assert '"X-Session-Key": session_key' not in src
