"""Field ⑤ — draft a GitHub pull request, never publish-ready, never merged.

Satisfies the spine's :class:`..spine.profile.PRRecipe` protocol (``namespace`` +
``draft``). Replaces the internal-review-CLI recipe this app was ported from;
``spine/profile.py`` names ``gh pr create --draft`` as the intended substitution,
so this is the seam working as designed.

Only the GitHub-specific seams live here: the ``gh`` argv, the PR-URL shape, and
the github.com HTTPS→SSH transport rewrite. The push/scan/queue machinery — the
safety-relevant half — is shared with the GitLab recipe in
:mod:`..pr_recipe_base`, whose module docstring explains why a push is needed at
all and how it is narrowed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

# Re-exports: established importers and tests reach the shared helpers through this
# module's historical names, and keeping them importable here means a provider-agnostic
# caller never has to know the machinery moved.
from ..pr_recipe_base import _CLI_TIMEOUT_S as _GH_TIMEOUT_S  # noqa: F401
from ..pr_recipe_base import (  # noqa: F401
    _PUSH_TIMEOUT_S,
    BRANCH_PREFIX,
    ProseRedactionUnavailable,
    ProviderPRRecipe,
    _kind_of,
    _redact_prose,
    _strip_leading_h1,
)

#: Draft-only PR creation. ``--draft`` is load-bearing: it is the mechanical half
#: of the draft-only policy the spine enforces. Never add ``--web`` (opens a
#: browser, useless headless) and never add a merge/ready subcommand here.
DRAFT_CMD = ("pr", "create", "--draft")

#: Shape of a real GitHub PR reference in ``gh`` stdout. ``gh pr create`` prints
#: the PR URL on success, but the clone can emit trailing chatter (git hooks, the
#: agent's own stdout) after it — the upstream app learned this the hard way when a
#: git hook's message got recorded as the review id. Scan every line for the
#: FIRST real PR URL rather than trusting the last line.
_PR_URL_RE = re.compile(r"https://(?:www\.)?github\.com/[^\s/]+/[^\s/]+/pull/(\d+)")


def extract_pr_url(stdout: str) -> str | None:
    """Return the first real GitHub PR URL in ``stdout``, else None."""
    match = _PR_URL_RE.search(stdout or "")
    return match.group(0) if match else None


#: ``https://github.com/<owner>/<repo>[.git]`` → the owner/repo pair.
_HTTPS_REMOTE_RE = re.compile(
    r"^https://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)


def _gh_prefers_ssh() -> bool:
    """True iff the ``gh`` CLI uses SSH for git operations against github.com.

    Read via ``gh config get`` rather than by parsing hosts.yml, so the answer
    comes from the tool that owns the setting.

    The HOST-SCOPED value is what matters and it is checked first: this setting is
    commonly per-host, and reading only the global default gets it backwards. On
    the host this was developed against, the global default is ``https`` while
    github.com is explicitly ``ssh`` — trusting the global answer alone would keep
    pushing over a transport that cannot authenticate.
    """
    if shutil.which("gh") is None:
        return False
    for args in (
        ["gh", "config", "get", "git_protocol", "-h", "github.com"],
        ["gh", "config", "get", "git_protocol"],
    ):
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return False
        value = (proc.stdout or "").strip().lower()
        if proc.returncode == 0 and value:
            return value == "ssh"
    return False


def _prefer_authenticated_remote(url: str) -> str:
    """Rewrite an HTTPS GitHub remote to SSH when that is what is authenticated.

    The clone is deliberately made over HTTPS (a validated, allowlisted URL —
    never raw user text), but the push has to actually authenticate. Observed
    live: an HTTPS push to github.com failed because git's global
    ``credential.helper`` on this host is bound to a different provider entirely,
    while ``gh`` was authenticated for SSH. Pushing over HTTPS in that setup can
    never succeed, so the PR silently degraded to the queue.

    Only ever rewrites the TRANSPORT of an already-validated github.com URL — the
    owner/repo come from the matched groups, so this cannot retarget the push.
    """
    match = _HTTPS_REMOTE_RE.match(url.strip())
    if not match or not _gh_prefers_ssh():
        return url
    return f"git@github.com:{match.group('owner')}/{match.group('repo')}.git"


class GitHubPRRecipe(ProviderPRRecipe):
    """Draft a GitHub PR from a push-disabled clone. Never publishes, never merges."""

    provider = "github"
    cli = "gh"

    def _authenticated_remote(self, url: str) -> str:
        """The github.com transport decision — see :func:`_prefer_authenticated_remote`."""
        return _prefer_authenticated_remote(url)

    def _create_cmd(self, *, summary: str, body_path: Path, branch: str) -> list[str]:
        """``gh pr create --draft`` argv. Draft-only; never ``--web``, never a merge verb."""
        cmd = [
            "gh",
            *DRAFT_CMD,
            "--title",
            summary,
            "--body-file",
            str(body_path),
            "--head",
            branch,
        ]
        if self.base_branch:
            cmd += ["--base", self.base_branch]
        return cmd

    def _extract_ref(self, stdout: str) -> str | None:
        """The first real PR URL in ``gh``'s stdout — see :data:`_PR_URL_RE`."""
        return extract_pr_url(stdout)
