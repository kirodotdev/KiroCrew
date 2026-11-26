"""Outbound URL vetting: the gateway's public-address rule for what a backend fetches.

:func:`vet_public_url` is ``kiro_crew.link_unfurl.vet_unfurl_url`` with no option:
an http(s) URL with no user name or password, on port 80 or 443, whose host is not a
``.onion`` or ``.local`` name and resolves, once, only to public addresses (an
alternate or tunnel spelling of an address is unwrapped first). It opens no
connection. The private function's ``resolve``, ``allow_any_port`` and
``allow_private`` each make it approve something the default refuses, which is the
gateway's decision for its own probes and never an app's, so they are not offered.

The check pins nothing by itself. A caller that vets a URL and then lets its HTTP
client resolve the name again has checked a different answer than the one it dials,
so dial only the result's ``addresses``, through a resolver keyed on its
``wire_host``.

:func:`address_is_not_public` is the same address rule for a value that is already a
resolution result, such as each hop of a redirect the caller resolved itself.
"""

from __future__ import annotations

from kiro_crew import link_unfurl

__all__ = ["UrlRejectedError", "address_is_not_public", "vet_public_url"]

#: Raised by :func:`vet_public_url` for a refused URL. ``.code`` is ``invalid_url``
#: (not an absolute http(s) URL, or one carrying credentials) or ``blocked_url``
#: (well formed, but refused by the address, port or host-suffix rule, or not
#: resolvable). The gateway's own class, so ``except UrlRejectedError`` catches what
#: it raises.
UrlRejectedError = link_unfurl.UnfurlRejected


def vet_public_url(url: str) -> link_unfurl.VettedUrl:
    """The vetted form of *url*, or :data:`UrlRejectedError` when it must not be fetched.

    The result carries the normalized ``url``, ``scheme``, ``host`` (for TLS and the
    ``Host`` header), ``wire_host`` (what the HTTP client hands its resolver),
    ``port``, ``ip``, ``addresses`` (every approved address, in resolver order,
    deduplicated) and ``domain``. Its class is the gateway's own and is not a public
    name: read the attributes, never import or construct it.

    ``kiro_crew.link_unfurl.vet_unfurl_url`` with its defaults: every address the
    name resolves to must be public, not just the first, so a host that answers with
    a public and a private address is refused. A name that does not resolve is
    refused too.

    Blocking: it performs one ``getaddrinfo``, so call it from a worker thread, never
    on an event loop. Connect only to ``.addresses``, with a resolver keyed on
    ``.wire_host`` that answers with them and nothing else; resolving the name again
    at connect time would dial an address this check never saw.
    """
    return link_unfurl.vet_unfurl_url(url)


def address_is_not_public(address: str) -> bool:
    """Whether *address*, an IP literal from a resolution result, must be refused.

    ``kiro_crew.link_unfurl.address_is_not_public``: ``True`` for any address that is
    not a globally reachable unicast one, with an IPv4-mapped or 6to4 spelling
    unwrapped first. A value that is not an IP literal also answers ``True``: a
    resolution result this check cannot read is one it cannot approve.
    """
    return bool(link_unfurl.address_is_not_public(address))
