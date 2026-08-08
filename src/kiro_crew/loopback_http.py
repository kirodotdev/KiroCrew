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

This module is a **stdlib-only leaf** and must stay one: it imports
``urllib.request`` and nothing else. That is what lets the cheapest callers use
it -- ``cron_trigger`` imports nothing from ``kiro_crew`` at all, and the MCP
stdio proxies deliberately avoid importing the gateway, since reaching
``parse_dashboard_url`` through ``dashboard.origin`` costs ~605 ms and 1124
modules (see the ``dashboard/urls.py`` docstring). Adding an import here that
pulls aiohttp in behind it would defeat the point.
"""

import urllib.request

__all__ = ["build_loopback_opener", "loopback_urlopen"]


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


def loopback_urlopen(req: urllib.request.Request | str, timeout: float):
    """Open ``req`` against the local gateway, ignoring any configured proxy.

    Drop-in for ``urllib.request.urlopen`` at loopback call sites, with one
    deliberate difference: ``timeout`` is **required**. A request to our own
    gateway that can hang forever is never what a call site wants, and making
    it explicit costs nothing -- every existing loopback site already passes
    one.

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
    return build_loopback_opener().open(req, timeout=timeout)
