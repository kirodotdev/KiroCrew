"""Provider-neutral error taxonomy shared by the GitHub and GitLab clients.

Issue Radar's routes were written against ``github_client``'s exception names
(``GhCliError`` / ``GhSetupError`` / ``GhPermissionError`` / ``RepoUrlError``)
and catch them in 26 places. Adding a second provider without forking every
one of those ``except`` clauses requires both clients to raise the *same*
classes -- not merely similar ones -- so the canonical definitions live here and
``github_client`` re-exports them under the historical ``Gh*`` names.

Those re-exports are ALIASES, not subclasses. A subclass would silently break
the thing this module exists to guarantee: ``except GhCliError`` would not catch
a GitLab failure, and every GitLab error would escape as a 500 instead of the
502/403 the route intends.
"""

from __future__ import annotations

import re

from kiro_crew.security import redact_local_paths

# Public provider endpoints are not host topology: the user is talking to them on
# purpose, and seeing the URL is often the point of the message. Any OTHER host in
# the text can identify a self-hosted enterprise instance.
_PUBLIC_HOSTS = ("github.com", "api.github.com", "gitlab.com")
_URL_RE = re.compile(r"https?://([^/\s'\"]+)[^\s'\"]*")
_REDACTED = "[redacted]"


def sanitize_cli_stderr(text: str) -> str:
    """Provider CLI stderr with host paths and private hosts removed.

    Provider CLI stderr reaches the browser verbatim through the routes' error
    bodies, so host topology is stripped first (CWE-209). Credentials are not a
    vector here -- both CLIs take their token from the environment, never argv, and
    neither echoes it -- so the goal is host detail, not secrets.

    Deliberately preserves the phrasing a user needs in order to fix the problem
    themselves -- ``gh auth login``, ``not found`` / ``Could not resolve to a
    Repository``, ``HTTP 403``, ``connection refused``, ``timeout`` -- because the
    frontend renders this text directly and a generic "upstream error" would leave
    the user unable to tell an auth problem from a typo'd repo name.
    """
    if not text:
        return ""
    out, _ = redact_local_paths(text)

    def _host(match: re.Match[str]) -> str:
        host = match.group(1).split("@")[-1].split(":")[0].lower()
        return match.group(0) if host in _PUBLIC_HOSTS else _REDACTED

    return _URL_RE.sub(_host, out)


class RepoUrlError(ValueError):
    """Raised when a repo URL is not a well-formed, supported provider URL.

    Callers map this to HTTP 400 (bad client input), as distinct from
    :class:`ProviderCliError`, which is an upstream problem (502).
    """


class ProviderCliError(RuntimeError):
    """Raised when a provider CLI is missing, unauthenticated, times out, or the
    API call fails."""


class ProviderSetupError(ProviderCliError):
    """Raised when the provider CLI cannot be used because the HOST is not set
    up -- the binary is absent (or not in a trusted directory), or the CLI has no
    authenticated session for the target host.

    Distinct from a generic :class:`ProviderCliError` (network blip, API 500)
    because the fix is a user action, not a retry: the connect dialog turns
    ``reason`` into install / login instructions instead of showing a raw error
    string. A subclass of ``ProviderCliError`` so existing handlers keep working.

    ``reason`` is ``"not_installed"`` or ``"not_authenticated"``.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class ProviderPermissionError(ProviderCliError):
    """Raised when the provider returns HTTP 403 because the caller lacks the
    required permission -- either a read endpoint out of reach (notably listing
    collaborators/members without push access) or a write call (label edit /
    close / reopen) rejected for want of the triage/push right.

    A subclass of :class:`ProviderCliError` so existing handlers still catch it,
    but distinguishable so callers can degrade gracefully: the members path falls
    back to the issue-derived set, and the write routes special-case it into an
    HTTP 403 rather than a generic 502.
    """


class PrSearchError(ValueError):
    """Raised when a pull/merge-request search query is not expressible against
    the target provider."""
