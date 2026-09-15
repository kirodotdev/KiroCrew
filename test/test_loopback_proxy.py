"""The loopback secret must never reach a proxy named by the environment.

Every request to the local gateway carries ``X-Internal-Secret``, which
authorises write endpoints. ``urlopen`` honours ``HTTP_PROXY`` for loopback
addresses -- urllib has no implicit loopback exemption -- so a developer or
container with a proxy configured leaks the secret to it.

The two env shapes guarded here are the two that actually leak, established by
running a real proxy listener and reading the bytes it received:

* proxy set, ``no_proxy`` unset
* proxy set, ``no_proxy=localhost``, target spelled ``127.0.0.1`` -- the
  dangerous one, because ``no_proxy=localhost`` is the common corporate default
  and looks like coverage while matching on the host string only

Both listeners are real sockets on port 0, following ``test_cron_trigger.py``:
the kernel hands out a free port, so no ``xdist_group`` marker is needed.

Deliberately NOT written as "call the product function and see where it went via
``urllib.request.urlopen``": ``urllib.request._opener`` is a process-global
cache built on the first ``urlopen`` call anywhere in the worker, so under
``-n auto`` a later ``monkeypatch.setenv`` is invisible through it and the test
would be order-dependent. The helper owns its own opener, and the control test
below builds a fresh one, so neither touches that cache.
"""

import http.server
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kiro_crew.loopback_http import build_loopback_opener, loopback_urlopen

CANARY = "canary-not-a-real-secret"
SECRET_HEADER = "X-Internal-Secret"

_PROXY_ENV_KEYS = (
    "http_proxy",
    "HTTP_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
    # getproxies_environment ignores uppercase HTTP_PROXY when REQUEST_METHOD is
    # set (the httpoxy CGI guard). Clear it so the parametrised spellings below
    # behave identically.
    "REQUEST_METHOD",
)


def _make_handler(sink, redirect_to=None):
    """Build a handler that records the secret it saw, optionally 302-ing away."""

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802 - stdlib naming
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            sink.append(
                {
                    "requestline": self.requestline,
                    "secret": self.headers.get(SECRET_HEADER),
                }
            )
            if redirect_to:
                self.send_response(302)
                self.send_header("Location", redirect_to)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, fmt, *args):
            pass  # keep pytest output clean

    return Handler


def _serve(sink, redirect_to=None):
    server = http.server.HTTPServer(("127.0.0.1", 0), _make_handler(sink, redirect_to))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _post(url):
    return urllib.request.Request(
        url,
        data=b"{}",
        headers={"Content-Type": "application/json", SECRET_HEADER: CANARY},
        method="POST",
    )


@pytest.fixture()
def listeners():
    """A stand-in gateway and a stand-in proxy, each recording what it received."""
    gateway_hits: list[dict] = []
    proxy_hits: list[dict] = []
    gateway, gateway_port = _serve(gateway_hits)
    proxy, proxy_port = _serve(proxy_hits)
    try:
        yield {
            "gateway_hits": gateway_hits,
            "proxy_hits": proxy_hits,
            "gateway_port": gateway_port,
            "proxy_port": proxy_port,
        }
    finally:
        gateway.shutdown()
        proxy.shutdown()


def _set_proxy_env(monkeypatch, proxy_port, spelling, no_proxy=None):
    for key in _PROXY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(spelling, f"http://127.0.0.1:{proxy_port}")
    if no_proxy is not None:
        monkeypatch.setenv("no_proxy", no_proxy)


class TestLoopbackSecretNeverReachesAProxy:
    """Regression guard for the loopback-secret-through-HTTP_PROXY leak."""

    @pytest.mark.parametrize("spelling", ["http_proxy", "HTTP_PROXY"])
    def test_ignores_proxy_when_no_proxy_is_unset(self, monkeypatch, listeners, spelling):
        """Probe case B: proxy set, no_proxy unset. The bare leak."""
        _set_proxy_env(monkeypatch, listeners["proxy_port"], spelling)

        with loopback_urlopen(
            _post(f"http://127.0.0.1:{listeners['gateway_port']}/api/lessons"), timeout=5
        ) as resp:
            assert resp.status == 200

        assert listeners["proxy_hits"] == [], (
            f"secret reached the proxy: {listeners['proxy_hits']}"
        )
        assert [h["secret"] for h in listeners["gateway_hits"]] == [CANARY]

    def test_ignores_proxy_when_no_proxy_names_localhost_only(self, monkeypatch, listeners):
        """Probe case F: no_proxy=localhost does NOT cover the literal 127.0.0.1.

        ``no_proxy`` matches the host string, not what it resolves to. This is
        the shape an operator is most likely to mistake for safety, so it gets
        its own test rather than a parameter.
        """
        _set_proxy_env(monkeypatch, listeners["proxy_port"], "http_proxy", no_proxy="localhost")

        with loopback_urlopen(
            _post(f"http://127.0.0.1:{listeners['gateway_port']}/api/lessons"), timeout=5
        ) as resp:
            assert resp.status == 200

        assert listeners["proxy_hits"] == [], (
            f"secret reached the proxy despite no_proxy=localhost: {listeners['proxy_hits']}"
        )
        assert [h["secret"] for h in listeners["gateway_hits"]] == [CANARY]

    def test_default_opener_does_leak(self, monkeypatch, listeners):
        """Canary for the premise: without the helper, the secret DOES proxy.

        Builds a fresh default opener rather than calling ``urlopen``, because
        the module-global opener is cached on first use per process and would
        make this order-dependent under xdist.

        If CPython ever adds an implicit loopback exemption this test fails,
        which is the signal to re-examine whether the helper is still needed --
        not a reason to delete the guard above.
        """
        _set_proxy_env(monkeypatch, listeners["proxy_port"], "http_proxy")

        with urllib.request.build_opener().open(
            _post(f"http://127.0.0.1:{listeners['gateway_port']}/api/lessons"), timeout=5
        ) as resp:
            assert resp.status == 200

        assert [h["secret"] for h in listeners["proxy_hits"]] == [CANARY]
        assert listeners["gateway_hits"] == [], "the proxy answered, so the gateway saw nothing"
        # Absolute-form request line is the confirmation it was treated as a
        # proxied host rather than a direct connection.
        assert listeners["proxy_hits"][0]["requestline"].startswith("POST http://127.0.0.1:")

    def test_refuses_to_follow_a_redirect(self, monkeypatch, listeners):
        """A 3xx from the gateway must not replay the secret to Location."""
        for key in _PROXY_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        redirect_hits: list[dict] = []
        target, target_port = _serve(redirect_hits)
        gateway_hits: list[dict] = []
        gateway, gateway_port = _serve(
            gateway_hits, redirect_to=f"http://127.0.0.1:{target_port}/stolen"
        )
        try:
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                loopback_urlopen(
                    _post(f"http://127.0.0.1:{gateway_port}/api/lessons"), timeout=5
                )
            assert excinfo.value.code == 302
            assert redirect_hits == [], f"secret followed the redirect: {redirect_hits}"
        finally:
            gateway.shutdown()
            target.shutdown()


class TestNoNewBareLoopbackSender:
    """A new secret-bearing call site must not be able to recreate this leak.

    The helper and its tests pin the helper, not the call sites -- so the next
    module that reaches for ``urllib.request.urlopen`` while sending a secret header
    silently reopens the hole, and the failure presents as a hang before it presents
    as a failure. This gate is what makes the fix stick.

    Deliberately line-level, not file-level: ``cli_server.py`` sends a secret AND
    legitimately fetches an external update feed, which must keep the proxy.
    """

    # A bare urlopen inside a secret-sending module is allowed only when one of the
    # 5 preceding lines names an external target. Semantic rather than exact-text so
    # an unrelated reformat does not fail CI, and explicit so adding one is a
    # deliberate edit with a reason.
    EXTERNAL_MARKERS = ("feed_url",)
    SECRET_HEADERS = ("X-Internal-Secret", "X-Local-Secret")

    def test_no_bare_urlopen_in_a_secret_sending_module(self):
        src = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
        assert src.is_dir(), f"source tree not found at {src}"

        offenders: list[str] = []
        for path in src.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if not any(h in text for h in self.SECRET_HEADERS):
                continue
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if "urllib.request.urlopen(" not in line:
                    continue
                window = "\n".join(lines[max(0, i - 5): i + 1])
                if any(m in window for m in self.EXTERNAL_MARKERS):
                    continue
                offenders.append(f"{path.relative_to(src)}:{i + 1}: {line.strip()}")

        assert offenders == [], (
            "these call sites send a secret header from a module that also uses a bare "
            "urlopen; route them through kiro_crew.loopback_http.loopback_urlopen, or add "
            "an EXTERNAL_MARKERS entry if the target is genuinely external:\n  "
            + "\n  ".join(offenders)
        )


class TestOpenerShape:
    """The opener must actually have proxies disabled, not merely appear to."""

    def test_opener_has_no_live_proxy_handler(self, monkeypatch, listeners):
        """Guards the ``build_opener`` displacement rule.

        ``build_opener`` drops a default handler only when a supplied handler
        subclasses it, and an empty ``ProxyHandler`` registers no
        ``<scheme>_open`` method -- so forgetting to pass it leaves the
        env-derived proxy live while the code still looks hardened.
        """
        _set_proxy_env(monkeypatch, listeners["proxy_port"], "http_proxy")
        live = [
            h
            for h in build_loopback_opener().handlers
            if isinstance(h, urllib.request.ProxyHandler) and h.proxies
        ]
        assert live == [], f"opener still carries a populated ProxyHandler: {live}"
