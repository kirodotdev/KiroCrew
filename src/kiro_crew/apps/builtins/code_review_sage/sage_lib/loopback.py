"""The proxy- and redirect-disabled loopback opener the Sage driver sends with.

Split out of ``review_driver`` unchanged; ``review_driver`` re-exports
``loopback_urlopen`` so callers keep using ``review_driver.loopback_urlopen``.
"""

from __future__ import annotations

import urllib.request

# Every loopback call the driver makes carries X-Internal-Secret, and urlopen
# honours HTTP_PROXY for loopback addresses, so a proxied environment would send
# the secret to the proxy in cleartext. The fallback must therefore stay
# proxy-disabled too -- degrading to a bare urlopen would leave the standalone
# path (`python3 sage_lib/review_driver.py`) carrying the leak this closes.
#
# It must ALSO refuse redirects, for the same reason the real helper does.
# "These endpoints never return a redirect" is wrong for `_probe`, whose entire
# job is to dial candidate ports that may have something other than our gateway
# listening. A 302 from one of those would replay the secret to whatever host
# Location names.
try:
    from kiro_crew.loopback_http import loopback_urlopen  # type: ignore
except ImportError:  # pragma: no cover - standalone fallback

    class _FallbackNoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def loopback_urlopen(req: urllib.request.Request | str, timeout: float):  # type: ignore
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _FallbackNoRedirect()
        ).open(req, timeout=timeout)


__all__ = ["loopback_urlopen"]
