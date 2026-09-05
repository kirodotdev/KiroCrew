"""Loopback HTTP requests with proxies and redirects disabled.

Every request Kiro Crew makes to its own gateway carries ``X-Internal-Secret``,
which authorises write endpoints on the local dashboard. ``urlopen`` builds its
opener from ``getproxies()``, and urllib has **no implicit loopback exemption**:
``proxy_bypass_environment`` returns ``False`` when ``no_proxy`` is unset, and
``ProxyHandler.proxy_open`` bypasses only the hosts ``no_proxy`` names outright.
So with ``HTTP_PROXY`` set -- routine in corporate and containerised
environments -- a request to ``http://127.0.0.1:5476`` is sent to the proxy in
absolute form, secret header included, in cleartext.

Two things that look like fixes and are not:

* Spelling the host ``127.0.0.1`` instead of ``localhost``. That addresses
  ``localhost`` resolving to ``::1`` past a v4-only listener -- a real problem,
  a different one. Both spellings proxy identically.
* Setting ``no_proxy=localhost``. ``no_proxy`` matches the host **string**, not
  what it resolves to, so a request to the literal IP still proxies. That
  spelling is the common corporate default, which makes it the more dangerous
  of the two: it looks like coverage.

Redirects are refused for the same reason the external bearer-token path in
``dashboard/handlers/kiro_usage_api.py`` refuses them -- a 3xx from the gateway
would replay the secret to whatever host ``Location`` names.

This module is a **stdlib-only leaf** and must stay one: it imports only
standard-library modules (``urllib.request``, ``http.client``, ``os``,
``socket``) and nothing from ``kiro_crew``. That is what lets the cheapest
callers use it -- ``cron_trigger`` imports nothing from ``kiro_crew`` at all,
and the MCP stdio proxies deliberately avoid importing the gateway, since
reaching ``parse_dashboard_url`` through ``dashboard.origin`` costs ~605 ms and
1124 modules (see the ``dashboard/urls.py`` docstring). Adding an import here
that pulls aiohttp in behind it would defeat the point.
"""

import http.client
import os
import socket
import urllib.error
import urllib.request

__all__ = ["build_loopback_opener", "loopback_urlopen", "unix_socket_urlopen"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn a 3xx into an ``HTTPError`` instead of following it.

    Returning ``None`` from ``redirect_request`` is urllib's documented way to
    refuse a redirect, so the secret is never replayed to a redirected host.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def build_loopback_opener() -> urllib.request.OpenerDirector:
    """Build an opener that ignores proxy environment variables entirely.

    ``ProxyHandler({})`` has to be passed **explicitly**. ``build_opener``
    displaces one of its default handlers only when a supplied handler
    *subclasses* that default, and an empty ``ProxyHandler`` registers no
    ``<scheme>_open`` method at all -- so omitting it leaves the env-derived
    proxy live. This is exactly why ``kiro_usage_api._build_opener`` still
    proxies (correctly, for its external target) despite looking similar.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class _UnixHTTPConnection(http.client.HTTPConnection):
    """``HTTPConnection`` whose transport is an ``AF_UNIX`` stream socket.

    The ``host`` from the request URL is kept (so the ``Host`` header the
    gateway's host-validation middleware sees is identical to the TCP path);
    only ``connect()`` is rerouted onto the unix socket.
    """

    def __init__(self, socket_path: str, host: str, timeout=None) -> None:
        if timeout is None:
            super().__init__(host)
        else:
            super().__init__(host, timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:  # noqa: D102 -- contract inherited
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout is not None:
            sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class _UnixHTTPHandler(urllib.request.HTTPHandler):
    """Route ``http://`` requests over a fixed ``AF_UNIX`` socket path.

    Subclasses ``HTTPHandler`` so ``build_opener`` displaces the default TCP
    handler (same displacement rule ``build_loopback_opener`` documents).
    """

    def __init__(self, socket_path: str) -> None:
        super().__init__()
        self._socket_path = socket_path

    def http_open(self, req):  # noqa: D102 -- contract inherited
        def _factory(host, timeout=None, **_kwargs):
            return _UnixHTTPConnection(self._socket_path, host, timeout=timeout)

        return self.do_open(_factory, req)


def _build_unix_opener(socket_path: str) -> urllib.request.OpenerDirector:
    """Opener twin of :func:`build_loopback_opener` bound to a unix socket."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect(), _UnixHTTPHandler(socket_path)
    )


def unix_socket_urlopen(
    req: urllib.request.Request | str,
    timeout: float,
    *,
    socket_path: "str | os.PathLike[str]",
):
    """Open ``req`` over *socket_path* and **only** over *socket_path*.

    The transport twin of :func:`loopback_urlopen` for callers whose request
    carries a credential that a different process must never receive. There is
    no TCP handler in this opener at all, so "no fallback" is structural rather
    than a flag someone can invert: a socket that is missing, stale, or refusing
    raises ``URLError`` and the request is not sent anywhere else.

    That distinction is the whole point. ``loopback_urlopen`` retries over TCP
    when nobody answers the socket, which is right for its callers -- they are
    reaching a gateway whose port they already trust, and a socket file left by a
    dead gateway should not break them. It is wrong for ``pod api``: a pod's port
    is ordinary loopback that any local user can bind the moment the pod
    releases it, so a TCP retry can hand the pod's minted token to whatever
    answered. A unix socket cannot be answered by another user, because it lives
    inside the pod's owner-only home.

    *socket_path* is trusted (derived, never caller-supplied); the URL's host is
    preserved, so the gateway's Host validation sees exactly what it would on
    TCP.
    """
    if not hasattr(socket, "AF_UNIX"):
        # Windows. Raised as OSError so callers' existing transport handling
        # catches it instead of an AttributeError escaping from connect().
        raise OSError("AF_UNIX sockets are not available on this platform")
    return _build_unix_opener(os.fspath(socket_path)).open(req, timeout=timeout)


def loopback_urlopen(
    req: urllib.request.Request | str,
    timeout: float,
    *,
    unix_socket_path: "str | os.PathLike[str] | None" = None,
):
    """Open ``req`` against the local gateway, ignoring any configured proxy.

    Drop-in for ``urllib.request.urlopen`` at loopback call sites, with one
    deliberate difference: ``timeout`` is **required**. A request to our own
    gateway that can hang forever is never what a call site wants, and making
    it explicit costs nothing -- every existing loopback site already passes
    one.

    When *unix_socket_path* is given and the file exists, the request is sent
    over that ``AF_UNIX`` socket instead of TCP (the URL -- and therefore the
    ``Host`` header -- is unchanged). This lets the gateway kernel-verify the
    caller's identity via ``SO_PEERCRED``; see ``dashboard/token_auth``. The
    TCP fallback triggers ONLY when nobody answers on the socket at connect
    time (``FileNotFoundError`` / ``ConnectionRefusedError``, i.e. a stale
    file left by a dead gateway): those cases provably never delivered the
    request, so retrying over TCP cannot double-send. Any later failure --
    an HTTP error status, a read timeout, a reset mid-response -- propagates
    exactly as it would on TCP, keeping every caller's error shape unchanged.

    A caller that must NOT fall back -- because its request carries a credential
    another process could capture on the port -- wants
    :func:`unix_socket_urlopen` instead. Do not add a "require unix" flag here:
    the guarantee then depends on a conditional every future edit can invert,
    and the callers below all want the fallback.

    The opener is rebuilt per call rather than cached at module scope. Both
    handlers are stateless, so a cache would be safe for the FIXED code -- but
    it would capture ``getproxies()`` at import time, making the module's
    behaviour depend on when it was first imported and silently defeating any
    test that sets a proxy variable afterwards. That is not hypothetical: the
    first version of this module cached the opener, and the regression tests
    passed even with ``ProxyHandler({})`` deleted. Constructing a few stateless
    handlers is far cheaper than the loopback round trip that follows.

    Use this for the local gateway ONLY. External requests must keep the
    default opener, because they genuinely need the corporate proxy to leave
    the host.
    """
    if unix_socket_path is not None and hasattr(socket, "AF_UNIX"):
        sock_path = os.fspath(unix_socket_path)
        if os.path.exists(sock_path):
            try:
                return unix_socket_urlopen(req, timeout, socket_path=sock_path)
            except urllib.error.HTTPError:
                # The gateway answered (4xx/5xx) -- a real response, never a
                # transport failure. HTTPError subclasses URLError, so this
                # re-raise must precede the URLError arm below.
                raise
            except urllib.error.URLError as exc:
                if not isinstance(exc.reason, (FileNotFoundError, ConnectionRefusedError)):
                    raise
                # Stale socket file (gateway died / restarted TCP-only):
                # connect never delivered anything, so TCP retry is safe.
    return build_loopback_opener().open(req, timeout=timeout)
