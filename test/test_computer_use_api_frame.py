"""POST /api/computer-use/frame, the live-view (PiP) ingress, and the relay that feeds it.

The ingress takes only a loopback POST carrying the verified secret and delivers
a frame to owner sockets only, and ``build_frame_payload`` bounds every field. The
capture side emits no frame for a secure window, an unattributable capture or an
empty snapshot, restores the frame scope after each dispatch and swallows a relay
error. The relay POST never goes through a proxy, and the invoke leg publishes the
calling surface's frame scope. The shared fixtures and app builders live in
``computer_use_api_helpers.py``.
"""

from __future__ import annotations

import dataclasses
import http.server
import threading
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from computer_use_api_helpers import (  # noqa: F401 -- fixtures are requested by name
    _VALID_FRAME,
    _client,
    _frame_app,
    _reset_ctx,
    home_fixture,
    mock_sel_fixture,
    no_agent_spec_rewrite,
    no_real_probe,
    profiles_dir_fixture,
)

from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.governance import parse_policy


def _install_ceiling(policy_body) -> None:
    """Compose a context carrying *policy_body* as the ceiling and install it."""
    from kiro_crew.config.loader import KiroCrewConfig

    base = build_default_context(KiroCrewConfig.load())
    ceiling = parse_policy(policy_body) if policy_body is not None else None
    ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))


class TestFrameIngress:
    """The ingress: loopback-gated, bounded, and OWNER-only on the way out."""

    @pytest.mark.asyncio
    async def test_a_non_loopback_post_is_refused(self, home, mock_sel):
        """The load-bearing auth control on this route.

        The body is a live frame of the operator's own desktop; an off-host POST
        has no legitimate caller, so it is refused before the body is even parsed.
        """
        app = _frame_app()
        delivered: list = []
        app["state"].deliver_ws_owners = lambda *a: delivered.append(a)
        with patch("kiro_crew.dashboard.origin.is_loopback", return_value=False):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/computer-use/frame", json=_VALID_FRAME)
        assert resp.status == 403
        assert delivered == []

    @pytest.mark.asyncio
    async def test_a_loopback_post_without_the_verified_secret_is_refused(self, home, mock_sel):
        """Loopback alone is NOT sufficient.

        Being listed in ``_STRICT_INTERNAL_API_PATHS`` does not prove the secret
        was checked: with the header absent the middleware falls through to cookie
        auth, and on a ``local_only=False`` deployment it reclassifies strict paths
        as "mixed". Either way a caller holding only a dashboard cookie or an
        app-scoped token would reach the handler — and could then inject arbitrary
        frames into every owner window's live view.
        """
        app = _frame_app(grant_internal_auth=False)
        delivered: list = []
        app["state"].deliver_ws_owners = lambda *a: delivered.append(a)
        with patch("kiro_crew.dashboard.origin.is_loopback", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/computer-use/frame", json=_VALID_FRAME)
        assert resp.status == 403
        assert delivered == []

    @pytest.mark.asyncio
    async def test_a_valid_frame_is_delivered_to_owner_sockets_only(self, home, mock_sel):
        """``deliver_ws_owners``, never ``broadcast_ws``.

        An App Kit credential can open ``/api/ws`` and lands in the all-clients
        set, so an all-clients broadcast of the operator's desktop would cross the
        App Kit boundary.
        """
        app = _frame_app()
        seen: list = []

        async def _deliver(event, payload):
            seen.append((event, payload))
            return 3

        app["state"].deliver_ws_owners = _deliver
        with patch("kiro_crew.dashboard.origin.is_loopback", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/computer-use/frame", json=_VALID_FRAME)
                body = await resp.json()
        assert resp.status == 200
        assert body == {"ok": True, "subscribers": 3}
        assert len(seen) == 1
        event, payload = seen[0]
        assert event == "computer_use_frame"
        assert payload["data"] == "QUJD" and payload["format"] == "jpeg"
        app["state"].broadcast_ws.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, home, mock_sel):
        app = _frame_app()
        with patch("kiro_crew.dashboard.origin.is_loopback", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/computer-use/frame",
                    data=b"not json",
                    headers={"Content-Type": "application/json"},
                )
        assert resp.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"format": "jpeg"},
            {"data": "", "format": "jpeg"},
            {"data": "QUJD"},  # no format at all
            {"data": "QUJD", "format": "png"},  # a full-res PNG is refused outright
            {"data": "QUJD", "format": "webp"},
            {"data": "<svg onload=alert(1)>", "format": "jpeg"},  # markup
            {"data": "http://evil.example/x", "format": "jpeg"},  # a URL
            {"data": "QUJD QUJD", "format": "jpeg"},  # whitespace
        ],
    )
    async def test_a_body_with_no_usable_jpeg_frame_is_400(self, body, home, mock_sel):
        """Nothing but a base64 JPEG travels this path.

        The PNG/WebP rows are the important ones: the encoder only ever produces a
        downscaled JPEG, so a frame claiming another encoding did not come from it
        and must be refused rather than relabelled.
        """
        app = _frame_app()
        delivered: list = []
        app["state"].deliver_ws_owners = lambda *a: delivered.append(a)
        with patch("kiro_crew.dashboard.origin.is_loopback", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/computer-use/frame", json=body)
        assert resp.status == 400
        assert delivered == []


class TestFramePayload:
    """``build_frame_payload`` — every field bounded at the boundary."""

    def test_a_minimal_valid_frame(self):
        from kiro_crew.computer_use.screencast import build_frame_payload

        assert build_frame_payload({"data": "QUJD", "format": "jpeg"}) == {
            "data": "QUJD",
            "format": "jpeg",
        }

    def test_padded_base64_is_accepted(self):
        from kiro_crew.computer_use.screencast import build_frame_payload

        out = build_frame_payload({"data": "QUJDRA==", "format": "jpeg"})
        assert out is not None and out["data"] == "QUJDRA=="

    def test_an_oversized_frame_is_rejected(self):
        """One frame must not become a multi-megabyte websocket write."""
        from kiro_crew.computer_use.screencast import (
            MAX_FRAME_B64_CHARS,
            build_frame_payload,
        )

        big = "Q" * (MAX_FRAME_B64_CHARS + 4)
        assert build_frame_payload({"data": big, "format": "jpeg"}) is None

    def test_dimensions_are_integers_within_the_encoder_ceiling(self):
        from kiro_crew.computer_use.screencast import build_frame_payload
        from kiro_crew.computer_use.types import MAX_SCREENSHOT_MAX_PX

        out = build_frame_payload(
            {"data": "QUJD", "format": "jpeg", "width": 1280, "height": "tall"}
        )
        assert out == {"data": "QUJD", "format": "jpeg", "width": 1280}
        # bool is an int subclass in Python, so an unguarded isinstance(int) would
        # let width=True through and the panel would treat it as a 1px frame.
        for bad in (True, False, 0, -5, MAX_SCREENSHOT_MAX_PX + 1):
            out = build_frame_payload(
                {"data": "QUJD", "format": "jpeg", "width": bad, "height": 800}
            )
            assert out is not None
            assert "width" not in out, f"width={bad!r} leaked"
            assert out["height"] == 800  # a valid sibling still passes

    def test_the_session_key_is_bounded_to_a_lookup_charset(self):
        from kiro_crew.computer_use.screencast import build_frame_payload

        out = build_frame_payload(
            {"data": "QUJD", "format": "jpeg", "session_key": "dashboard:slot1"}
        )
        assert out is not None and out["session_key"] == "dashboard:slot1"
        for bad in (123, "has space", "x" * 129, "<b>"):
            out = build_frame_payload({"data": "QUJD", "format": "jpeg", "session_key": bad})
            assert out is not None and "session_key" not in out

    def test_the_app_label_is_bounded(self):
        from kiro_crew.computer_use.screencast import build_frame_payload

        out = build_frame_payload({"data": "QUJD", "format": "jpeg", "app": "Google Chrome"})
        assert out is not None and out["app"] == "Google Chrome"
        for bad in (7, "x" * 97, "</span><script>", "line\nbreak"):
            out = build_frame_payload({"data": "QUJD", "format": "jpeg", "app": bad})
            assert out is not None and "app" not in out


class TestLiveViewSuppression:
    """The three suppressions that make this pixel egress path acceptable.

    All evaluated in ``computer_use/screencast.py`` BEFORE anything leaves the
    process, so a suppressed frame is never even POSTed — the ingress handler is
    not the boundary and must not be relied on as one.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-scoped autouse fixture, moved verbatim; no test in this class calls monkeypatch.undo()
    def no_real_post(self, monkeypatch):
        """Record POST attempts instead of making them."""
        from kiro_crew.computer_use import screencast

        posted: list[dict] = []
        monkeypatch.setattr(screencast, "_post_frame", lambda payload: posted.append(payload))
        # ``emit_snapshot_frame`` spawns the POST on a daemon thread; run it inline
        # so the assertions are deterministic.
        monkeypatch.setattr(
            screencast.threading,
            "Thread",
            lambda target=None, args=(), **_kw: _InlineThread(target, args),
        )
        return posted

    def _snapshot(self, *, secure: bool = False):
        from kiro_crew.computer_use.types import AppRef, Snapshot

        return Snapshot(
            app=AppRef(name="Finder", pid=41, bundle_id="com.apple.finder", window_id=9),
            window_title="Documents",
            has_secure=secure,
            image_jpeg=b"\xff\xd8\xff\xd9",
            image_path="/tmp/shot.jpeg",
            image_width=1280,
            image_height=800,
        )

    def test_a_secure_window_emits_no_frame(self, home, profiles_dir, no_real_post):
        """A window holding a password field is NEVER mirrored.

        Read from ``Snapshot.has_secure`` — the driver's own predicate, the same
        one ``capture_snapshot_image`` refuses on — rather than re-derived here, so
        there is exactly one definition of "this window holds a secure field".
        """
        from kiro_crew.computer_use import screencast

        _install_ceiling(None)
        with screencast.frame_scope(session_key="dashboard:slot1"):
            assert screencast.emit_snapshot_frame(self._snapshot(secure=True)) is False
        assert no_real_post == []

    def test_a_capture_with_no_published_scope_emits_no_frame(
        self, home, profiles_dir, no_real_post
    ):
        """Fail-closed by construction: an unattributable frame is not mirrored.

        The capture layer has no session identity of its own, so the invoke handler
        publishes one for the duration of a dispatch. A capture reached any other
        way (a CLI probe, a future caller that skipped the handler) cannot be
        governed for a surface, so it emits nothing rather than guessing.
        """
        from kiro_crew.computer_use import screencast

        _install_ceiling(None)
        assert screencast.active_scope() is None
        assert screencast.emit_snapshot_frame(self._snapshot()) is False
        assert no_real_post == []

    def test_a_snapshot_with_no_encoded_bytes_emits_no_frame(
        self, home, profiles_dir, no_real_post
    ):
        from kiro_crew.computer_use import screencast
        from kiro_crew.computer_use.types import AppRef, Snapshot

        _install_ceiling(None)
        empty = Snapshot(app=AppRef(name="Finder", pid=41, window_id=9))
        with screencast.frame_scope(session_key="dashboard:slot1"):
            assert screencast.emit_snapshot_frame(empty) is False
        assert no_real_post == []

    def test_the_frame_scope_is_restored_and_never_leaks_across_dispatches(self, home):
        """A pooled worker thread must not carry one surface's identity forward."""
        from kiro_crew.computer_use import screencast

        assert screencast.active_scope() is None
        with screencast.frame_scope(session_key="dashboard:slot1", agent="kirocrew"):
            outer = screencast.active_scope()
            assert outer is not None and outer.session_key == "dashboard:slot1"
            with screencast.frame_scope(session_key="cron:nightly"):
                inner = screencast.active_scope()
                assert inner is not None and inner.session_key == "cron:nightly"
            # Restored to the enclosing scope, not cleared.
            restored = screencast.active_scope()
            assert restored is not None and restored.session_key == "dashboard:slot1"
        assert screencast.active_scope() is None

    def test_a_relay_error_is_swallowed(self, home, profiles_dir, monkeypatch, no_real_post):
        """The mirror must never degrade the observation the model asked for.

        ``emit_snapshot_frame`` is called from the capture path, so an unexpected
        failure anywhere inside it (here: the scope lookup) has to become a dropped
        frame rather than a failed screenshot.
        """
        from kiro_crew.computer_use import screencast

        _install_ceiling(None)

        def _boom():
            raise RuntimeError("scope lookup on fire")

        monkeypatch.setattr(screencast, "active_scope", _boom)
        assert screencast.emit_snapshot_frame(self._snapshot()) is False
        assert no_real_post == []


class TestTheFrameRelayIsNeverProxied:
    """The relay POST carries the gateway's local secret, so it must not proxy.

    ``urlopen`` builds its opener from ``getproxies()`` and urllib has no implicit
    loopback exemption, so with ``http_proxy`` set — routine in a container or on a
    corporate desktop — a POST to ``http://127.0.0.1:<port>`` goes to the proxy in
    absolute form with ``X-Internal-Secret`` attached, in cleartext. The relay runs
    through ``loopback_urlopen``, whose opener carries an explicit
    ``ProxyHandler({})``.

    ``_post_frame`` is called DIRECTLY rather than through ``emit_snapshot_frame``:
    every other case in this file replaces ``_post_frame`` wholesale, so the
    transport inside it is the one part of the relay with no coverage at all.

    Both listeners are real sockets on port 0, following ``test_cron_trigger.py`` —
    the kernel hands out a free port, so no ``xdist_group`` marker is needed.
    """

    SECRET = "canary-not-a-real-frame-secret"

    # ``getproxies_environment`` ignores uppercase ``HTTP_PROXY`` when
    # ``REQUEST_METHOD`` is set (the httpoxy CGI guard), so that is cleared too —
    # otherwise a CI runner exporting it would make this pass vacuously.
    _PROXY_ENV_KEYS = (
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
        "no_proxy",
        "NO_PROXY",
        "REQUEST_METHOD",
    )

    @staticmethod
    def _serve(sink: list):
        """A listener that records the secret header it saw, then answers 200."""

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                sink.append(
                    {
                        "requestline": self.requestline,
                        "secret": self.headers.get("X-Internal-Secret"),
                    }
                )
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, fmt, *args):
                pass  # keep pytest output clean

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, server.server_address[1]

    def test_the_secret_reaches_the_gateway_and_never_the_proxy(self, home, monkeypatch):
        from kiro_crew.computer_use import screencast

        gateway_hits: list[dict] = []
        proxy_hits: list[dict] = []
        gateway, gateway_port = self._serve(gateway_hits)
        proxy, proxy_port = self._serve(proxy_hits)
        try:
            for key in self._PROXY_ENV_KEYS:
                monkeypatch.delenv(key, raising=False)
            monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy_port}")
            # The real ``_headers()`` runs, so the header the ingress authenticates
            # with is the one under test rather than a literal in this file.
            (home / ".local_secret").write_text(self.SECRET, encoding="utf-8")
            monkeypatch.setattr(
                screencast,
                "_ingress_url",
                lambda: f"http://127.0.0.1:{gateway_port}{screencast.FRAME_INGRESS_PATH}",
            )

            screencast._post_frame({"data": "QUJD", "format": "jpeg"})

            assert proxy_hits == [], f"the frame secret reached the proxy: {proxy_hits}"
            # Positive leg too: ``_post_frame`` swallows every failure, so "the proxy
            # saw nothing" alone would also pass if the POST had never happened.
            assert [h["secret"] for h in gateway_hits] == [self.SECRET]
            # Origin-form request line confirms a direct connection rather than a
            # proxied one, which is sent absolute-form.
            assert gateway_hits[0]["requestline"].startswith(
                f"POST {screencast.FRAME_INGRESS_PATH}"
            )
        finally:
            gateway.shutdown()
            proxy.shutdown()


class TestInvokePublishesTheFrameScope:
    """The invoke leg is what makes a capture attributable to a surface."""

    @pytest.mark.asyncio
    async def test_the_dispatch_runs_inside_the_calling_surfaces_frame_scope(
        self, home, monkeypatch
    ):
        from kiro_crew.computer_use import screencast

        seen: dict = {}

        def _fake_dispatch(tool_name, args, *, session_key, agent="", app="", **_kw):
            scope = screencast.active_scope()
            seen["scope"] = scope
            return "ok"

        monkeypatch.setattr("kiro_crew.computer_use.tools.dispatch_tool", _fake_dispatch)
        async with _client() as client:
            await client.post(
                "/api/computer-use/invoke",
                json={
                    "tool": "computer_get_state",
                    "args": {"app": "Finder"},
                    "session_key": "dashboard:slot1",
                    "agent": "kirocrew",
                    "app": "notes",
                },
            )
        scope = seen["scope"]
        assert scope is not None
        assert (scope.session_key, scope.agent, scope.app) == (
            "dashboard:slot1",
            "kirocrew",
            "notes",
        )
        # Restored once the dispatch returns, so the pooled worker is clean.
        assert screencast.active_scope() is None


class _InlineThread:
    """Minimal ``threading.Thread`` stand-in that runs the target on ``start()``."""

    def __init__(self, target, args):
        self._target = target
        self._args = args
        self.daemon = False
        self.name = ""

    def start(self) -> None:
        if self._target is not None:
            self._target(*self._args)
