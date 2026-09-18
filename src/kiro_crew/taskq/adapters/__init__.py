"""Built-in dependency adapters: service error shape -> :class:`DependencySignal`.

Each adapter module exposes ``classify(exc, scope) -> DependencySignal | None``
and is registered by :func:`install` in the order below. Order matters only
where two adapters could both match: GitHub first (it reads GitHub-specific
headers and ``gh`` wording), the generic HTTP adapter second (status codes and
``Retry-After`` on any HTTP error), the ACP/provider adapter last (it reads
the provider stream's own error vocabulary).
"""

from __future__ import annotations

from ..dependency import register_adapter
from . import acp_provider, github, http

ADAPTER_GITHUB = "github"
ADAPTER_HTTP = "http"
ADAPTER_ACP_PROVIDER = "acp_provider"


def install() -> None:
    register_adapter(ADAPTER_GITHUB, github.classify)
    register_adapter(ADAPTER_HTTP, http.classify)
    register_adapter(ADAPTER_ACP_PROVIDER, acp_provider.classify)


__all__ = [
    "ADAPTER_ACP_PROVIDER",
    "ADAPTER_GITHUB",
    "ADAPTER_HTTP",
    "acp_provider",
    "github",
    "http",
    "install",
]
