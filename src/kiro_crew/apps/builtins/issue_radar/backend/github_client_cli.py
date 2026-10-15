"""The ``gh`` chokepoints every GitHub call goes through.

The provider-shared exception aliases, the call timeouts, the hardened ``gh``
spawn and the REST read runner. Callers and tests reach every name here through
:mod:`kiro_crew.apps.builtins.issue_radar.backend.github_client`, so a patch of ``_gh_run`` or ``_run_gh_api`` there
reaches every caller.
"""

from __future__ import annotations

import subprocess

from . import github_transport
from .errors import (
    ProviderCliError,
    ProviderInvalidInputError,
    ProviderMergeRefusedError,
    ProviderPermissionError,
    ProviderSetupError,
    sanitize_cli_stderr,
)

# ── exception aliases ────────────────────────────────────────────────────────
#
# The canonical classes live in ``errors`` so ``gitlab_client`` raises the SAME
# objects and ``routes.py``'s 26 ``except github_client.GhCliError`` clauses keep
# working for both providers. These are aliases, NOT subclasses: a subclass would
# mean a GitLab failure escaped those handlers as an unhandled 500.
#
# The historical names are kept because they are the documented surface every
# route and test already uses:
#
#   GhCliError        -- gh is missing, unauthenticated, timed out, or the API failed
#   GhSetupError      -- the HOST is not set up (binary absent / no auth session);
#                        carries ``reason`` ("not_installed" | "not_authenticated")
#                        so the connect dialog offers instructions, not a raw error
#   GhPermissionError -- HTTP 403 for want of a permission, so the members path can
#                        fall back to the derived roster and writes can map to 403
#   GhInvalidInputError -- the provider REJECTED a value in the request (e.g. an
#                        unassignable login, GitHub 422); maps to 400, not 502,
#                        because the forge is fine and the input is not
#   RepoUrlError      -- the URL is not a well-formed repo link (maps to 400)
GhCliError = ProviderCliError
GhSetupError = ProviderSetupError
GhPermissionError = ProviderPermissionError
GhInvalidInputError = ProviderInvalidInputError
GhMergeRefusedError = ProviderMergeRefusedError


GH_TIMEOUT_SEC = 20.0
# Open issues are loaded in FULL via --paginate, which can span many pages on a
# busy repo (kirodotdev/Kiro ~2.6k open → ~26 pages), so it gets a much larger
# budget than the single-shot calls. The result is cached, so this cost is paid
# once per refresh, not per view.
GH_PAGINATE_TIMEOUT_SEC = 120.0


# ── gh spawn hardening ───────────────────────────────────────────────────────
#
# These gh calls are benign-allowlisted in the repo's spawn audit: gh needs the
# host's OWN authenticated session and cannot be sandbox-routed (the sandbox
# would hide ~/.config/gh + the keychain, breaking auth). As defense-in-depth
# WITHIN that classification, every spawn goes through ``_gh_run``, which
# (1) resolves a validated ``gh`` (never a shim on the agent-writable
# front of PATH) and (2) hands the child a MINIMAL environment — PATH/HOME/XDG
# plus gh's own auth/network vars — instead of the gateway's full env, so
# unrelated secrets (AWS/Slack/SSH) can never leak to a substituted or
# compromised gh.

_GH_OVERRIDE_ENV = github_transport.GH_OVERRIDE_ENV


def _gh_bin() -> str:
    """Absolute path to an acceptable ``gh``, resolved and cached by the shared
    runner (``github_runner.resolve_gh``): the well-known install dirs first,
    then the ambient ``PATH``, accepting the user's own install (Homebrew
    included) while refusing a binary owned by another user, a world-writable
    one, or one inside the agent-writable project/workspace tree. Set
    ``KIROCREW_ISSUE_RADAR_GH`` to an absolute path to override (still
    validated), or ``KIROCREW_PROVIDER_BIN_STRICT=1`` to require a root-owned
    ``gh``. Raises :class:`GhSetupError` if no acceptable executable is found."""
    return github_transport.resolve_binary(override_env=_GH_OVERRIDE_ENV)


def _stderr_tail(proc: subprocess.CompletedProcess) -> str:
    """Last few stderr lines, sanitized for display.

    These strings travel to the browser through the routes' error bodies, so host
    paths and private hosts are stripped while the actionable phrasing (auth,
    not-found, 403, timeout) is preserved.
    """
    return github_transport.stderr_tail(proc, sanitize=sanitize_cli_stderr)


def _gh_run(
    argv: list[str], *, timeout: float, input_text: str | None = None
) -> subprocess.CompletedProcess:
    """Single Issue Radar chokepoint for every ``gh`` call — delegates to the
    shared hardened runner (``github_runner.run_gh``): validated gh as
    argv[0], minimal env, bounded timeout, and an SEL tool-invocation event on
    success, failure, and timeout. This wrapper keeps Issue Radar's error
    taxonomy (GhSetupError/GhCliError) so routes and the connect dialog are
    untouched."""
    return github_transport.run(
        argv,
        timeout=timeout,
        input_text=input_text,
        gh_bin=_gh_bin,
    )


def _run_gh_api(
    path: str, jq_filter: str, *, timeout: float = GH_TIMEOUT_SEC, paginate: bool = True
) -> list[dict]:
    """Run ``gh api <path> --jq <filter>`` and parse JSONL stdout.

    List argv only (never ``shell=True``); ``path`` must already be built from
    charset-validated owner/repo segments by the caller. ``paginate`` follows
    ``Link`` headers to fetch every page (used for open issues/labels); pass
    ``paginate=False`` to cap at a single ``per_page`` page (used for closed
    issues, which can number in the thousands).
    """
    return github_transport.run_api(
        path,
        jq_filter,
        timeout=timeout,
        paginate=paginate,
        gh_run=_gh_run,
        auth_classifier=_raise_if_auth_failure,
        sanitize=sanitize_cli_stderr,
    )


# Markers `gh` prints when the CLI itself has no usable credentials (as opposed
# to a repo simply being out of reach for an authenticated user). Matched
# case-insensitively against the stderr tail so the connect dialog can offer
# `gh auth login` instead of echoing an opaque exit code.
_GH_AUTH_MARKERS = github_transport.GH_AUTH_MARKERS


def _raise_if_auth_failure(stderr_tail: str) -> None:
    """Re-classify an unauthenticated `gh` failure as :class:`GhSetupError`.

    No-op for every other failure, so the caller still raises its own, more
    specific ``GhCliError`` with the full context.
    """
    github_transport.raise_if_auth_failure(stderr_tail, markers=_GH_AUTH_MARKERS)
