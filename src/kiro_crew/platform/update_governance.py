"""The update seam: where new code may come from, and the minimum version.

Two enterprise pins, read from the trust-root ``security_policy.json`` (see
``governance.UpdatePins``), applied at the three places KiroCrew replaces its own
code: ``POST /api/update``, ``kirocrew update``, and the unattended gateway-boot
auto-apply. This module exists so those three share one implementation and
cannot drift.

Deliberately NOT a governance archetype: a remote URL and a version number are
values the core consumes, not "is X permitted?" decisions, so they need no
``SCOPE_CATALOG`` row, no matcher, and no evaluator change.

**A pin blocks; an unresolvable pin does not.** If governance cannot be read at
all the update proceeds — refusing one would strand a host on a build that may
need a patch, and the pins are a routing constraint, not a security boundary
against a local operator who could edit the checkout directly.
"""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECS = 10


def resolve_remote_url(proj: str, *, remote: str = "", branch: str = "") -> str:
    """The URL of the remote an update would fetch from in *proj* (``""`` if unknown).

    Pass *remote* for a FIXED remote name: the CLI and gateway paths run
    ``git fetch origin <branch>``, so they must validate ``origin`` rather than
    whatever the branch tracks. With no *remote*, ``branch.<name>.remote`` is
    resolved instead — that is the API path's bare ``git pull``, which follows the
    tracked remote. Validating a different remote than the one fetched would
    approve one source and install another.

    ``ls-remote --get-url`` (not ``remote get-url``) additionally applies
    ``url.<base>.insteadOf`` rewriting, so the value checked is the URL git
    resolves rather than the one merely written down.

    Returns ``""`` on any failure, which a source pin then treats as "not
    permitted" and an unpinned host ignores.
    """

    def _git(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", *args],
                cwd=proj,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SECS,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return out.stdout.strip() if out.returncode == 0 else ""

    if not remote:
        if not branch:
            branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        if not branch or branch == "HEAD":  # detached HEAD tracks nothing
            return ""
        remote = _git("config", "--get", f"branch.{branch}.remote") or "origin"
    url = _git("ls-remote", "--get-url", "--", remote)
    # `--get-url` echoes its argument back for an unknown remote; that is a bare
    # name, not a URL, and must not be checked as one.
    return "" if url == remote else url


def update_blocked_reason(remote_url: str) -> str:
    """Why this update is not allowed, or ``""`` when it is.

    The one gate the three update paths call. The returned string is
    operator-facing (it reaches an API 403 body and the CLI's stderr), so it names
    neither the remote nor the pin: a git remote can embed a token
    (``https://x-access-token:<pat>@host/…``, ``?access_token=…``) and so can the
    pin. The operator can read both from `git remote -v` and the policy file; the
    log/response needs only to say which check failed.
    """
    from kiro_crew.platform.governance import active_update_pins

    if not active_update_pins().permits_source(remote_url):
        return (
            "this checkout's git remote does not match the update source pinned "
            "by the security policy"
        )
    return ""


def update_required(current_version: str) -> bool:
    """Is this build below the fleet's pinned minimum version?

    ``True`` makes an update MANDATORY — it overrides the user's
    ``auto_update=False``, because user config sits under the enterprise ceiling
    and an operator opting out must not be able to hold a fleet on a build the
    policy forbids. It never refuses to boot: bricking a fleet on a policy typo
    would remove the very surface an admin needs to fix it.
    """
    from kiro_crew.platform.governance import active_update_pins

    return not active_update_pins().meets_min_version(current_version)


def min_version() -> str:
    """The pinned minimum version, or ``""`` when unpinned (for display)."""
    from kiro_crew.platform.governance import active_update_pins

    return active_update_pins().min_version


__all__ = ["resolve_remote_url", "update_blocked_reason", "update_required", "min_version"]
