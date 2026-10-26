"""Field ⑤ — draft a GitLab merge request, never publish-ready, never merged.

The GitLab twin of :mod:`..github_repo.pr_recipe`. Only the provider seams live
here: the ``glab`` argv, the MR-URL shape (GitLab nests projects, so the path can
be ``group/subgroup/project``), and the per-host HTTPS→SSH transport rewrite. The
push/scan/queue machinery — the safety-relevant half — is shared with the GitHub
recipe in :mod:`..pr_recipe_base`, whose module docstring explains why a push is
needed at all and how it is narrowed.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from .. import GITHUB_HOSTS
from ..pr_recipe_base import ProviderPRRecipe
from .glab_cli import glab_env
from .glab_cli import glab_prefers_ssh as _glab_prefers_ssh

#: Draft-only MR creation. ``--draft`` is the mechanical half of the draft-only
#: policy the spine enforces. Never add a merge/ready/approve verb here, and keep
#: ``--yes`` in the argv builder: ``glab`` prompts interactively for anything it
#: can't infer, and a headless draft must never hang on a tty question.
DRAFT_MR_CMD = ("mr", "create", "--draft")

#: Shape of a real GitLab MR reference in ``glab`` stdout. GitLab nests projects
#: (``group/subgroup/project``), so the path is one-or-more segments before the
#: literal ``/-/merge_requests/<n>`` marker — the marker is what keeps prose and
#: GitHub pull URLs from ever matching. Host is deliberately unpinned: self-managed
#: GitLab lives on arbitrary domains. Scan for the FIRST real URL, not the last
#: line, for the same trailing-chatter reason as the GitHub recipe.
_MR_URL_RE = re.compile(r"https://[^\s/]+/(?:[^\s/]+/)+-/merge_requests/(\d+)")


def extract_mr_url(stdout: str) -> str | None:
    """Return the first real GitLab MR URL in ``stdout``, else None."""
    match = _MR_URL_RE.search(stdout or "")
    return match.group(0) if match else None


#: ``https://<host>/<nested/project/path>[.git]`` → host + project path.
_HTTPS_GITLAB_RE = re.compile(r"^https://(?P<host>[^/\s]+)/(?P<path>[^\s]+?)(?:\.git)?/?$")


def prefer_authenticated_gitlab_remote(url: str) -> str:
    """Rewrite an HTTPS GitLab remote to SSH when that is what ``glab`` authenticates.

    Same rationale as the GitHub rewrite: the clone URL is validated HTTPS, but the
    push has to authenticate with whatever transport the CLI is configured for.
    Only ever rewrites the TRANSPORT — host and project path come from the matched
    groups, so this cannot retarget the push. GitHub hosts pass through untouched;
    that transport decision belongs to the GitHub recipe.
    """
    match = _HTTPS_GITLAB_RE.match(url.strip())
    if not match:
        return url
    host = match.group("host").lower()
    # GitHub hosts belong to the GitHub recipe. scp-like ssh has no port slot, so a
    # port-carrying self-managed host keeps its https url.
    if host in GITHUB_HOSTS or ":" in host or not _glab_prefers_ssh(host):
        return url
    return f"git@{host}:{match.group('path')}.git"


def _remote_host(url: str) -> str:
    """The network host (with a non-443 port) a remote url names, or ``""``."""
    raw = (url or "").strip()
    if raw.startswith("git@"):
        return raw[len("git@") :].partition(":")[0].lower().rstrip(".")
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme != "https" or not host:
        return ""
    return f"{host}:{port}" if port and port != 443 else host


class GitLabMRRecipe(ProviderPRRecipe):
    """Draft a GitLab MR from a push-disabled clone. Never publishes, never merges."""

    provider = "gitlab"
    cli = "glab"

    def _authenticated_remote(self, url: str) -> str:
        """The per-host transport decision — see :func:`prefer_authenticated_gitlab_remote`."""
        return prefer_authenticated_gitlab_remote(url)

    def _cli_env(self) -> dict[str, str]:
        """``glab``'s child env, HOST-PINNED to the push destination's host.

        The MR is created on the host the fix branch was just pushed to, so that host
        is the one ``GITLAB_HOST`` names, and the gitlab.com token is withheld unless
        it IS gitlab.com (see :func:`.glab_cli.glab_env`). An unresolvable destination
        pins nothing and strips the token: fail closed.
        """
        return glab_env(_remote_host(self._resolve_fetch_url() or ""))

    def _create_cmd(self, *, summary: str, body_path: Path, branch: str) -> list[str]:
        """``glab mr create --draft`` argv. Draft-only; ``--yes`` keeps it headless."""
        cmd = [
            "glab",
            *DRAFT_MR_CMD,
            "--title",
            summary,
            "--description-file",
            str(body_path),
            "--source-branch",
            branch,
        ]
        if self.base_branch:
            cmd += ["--target-branch", self.base_branch]
        # The clone's own origin is neutralized, so glab cannot find the project from
        # git. Name it with the validated push destination the branch just went to
        # (glab accepts a full or git URL here).
        destination = self._resolve_fetch_url()
        if destination:
            cmd += ["--repo", destination]
        # Last, so every prompt glab would raise is already answered by the flags above.
        cmd.append("--yes")
        return cmd

    def _extract_ref(self, stdout: str) -> str | None:
        """The first real MR URL in ``glab``'s stdout — see :data:`_MR_URL_RE`."""
        return extract_mr_url(stdout)
