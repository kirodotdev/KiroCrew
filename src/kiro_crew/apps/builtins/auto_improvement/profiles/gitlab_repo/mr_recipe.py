"""Field ⑤ — draft a GitLab merge request, never publish-ready, never merged.

The GitLab twin of :mod:`..github_repo.pr_recipe`. Only the provider seams live
here: the ``glab`` argv, the MR-URL shape (GitLab nests projects, so the path can
be ``group/subgroup/project``), and the per-host HTTPS→SSH transport rewrite. The
push/scan/queue machinery — the safety-relevant half — is shared with the GitHub
recipe in :mod:`..pr_recipe_base`, whose module docstring explains why a push is
needed at all and how it is narrowed.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse, urlsplit

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
_HTTPS = "https://"
_DIGITS = frozenset("0123456789")
#: The separator of a URL PATH (always '/', on every OS) — never a filesystem path.
_URL_SEP = "/"


def _mr_url_at(candidate: str) -> str | None:
    """The MR URL ``candidate`` starts with, cut after the MR number, else None.

    Parsed with ``urlsplit``; the path is split on ``/`` and the ``-``,
    ``merge_requests`` and number segments are compared as exact tokens, after at
    least one non-empty namespace/project segment.
    """
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.netloc:
        return None
    segs = parts.path.split(_URL_SEP)
    # segs[0] is the empty text before the leading "/".
    for i in range(2, len(segs) - 2):
        if segs[i] != "-" or segs[i + 1] != "merge_requests":
            continue
        if not all(segs[1:i]):
            return None
        number = ""
        for ch in segs[i + 2]:
            if ch not in _DIGITS:
                break
            number += ch
        if not number:
            return None
        return f"{_HTTPS}{parts.netloc}{_URL_SEP.join(segs[: i + 2])}{_URL_SEP}{number}"
    return None


def extract_mr_url(stdout: str) -> str | None:
    """Return the first real GitLab MR URL in ``stdout``, else None."""
    for token in (stdout or "").split():
        start = token.find(_HTTPS)
        while start != -1:
            found = _mr_url_at(token[start:])
            if found:
                return found
            start = token.find(_HTTPS, start + 1)
    return None


def _https_host_and_path(url: str) -> tuple[str, str] | None:
    """``https://<host>/<nested/project/path>[.git][/]`` → (host, project path), else None.

    Parsed with ``urlsplit``. A query, fragment or whitespace means it is not a plain
    remote, so it is left alone.
    """
    value = url.strip()
    if any(c.isspace() for c in value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
        return None
    if not value.startswith(_HTTPS):
        return None
    path = parts.path[1:]
    path = path.removesuffix("/")
    path = path.removesuffix(".git")
    if not path:
        return None
    return parts.netloc, path


def prefer_authenticated_gitlab_remote(url: str) -> str:
    """Rewrite an HTTPS GitLab remote to SSH when that is what ``glab`` authenticates.

    Same rationale as the GitHub rewrite: the clone URL is validated HTTPS, but the
    push has to authenticate with whatever transport the CLI is configured for.
    Only ever rewrites the TRANSPORT — host and project path come from the matched
    groups, so this cannot retarget the push. GitHub hosts pass through untouched;
    that transport decision belongs to the GitHub recipe.
    """
    found = _https_host_and_path(url)
    if found is None:
        return url
    host, path = found[0].lower(), found[1]
    # GitHub hosts belong to the GitHub recipe. scp-like ssh has no port slot, so a
    # port-carrying self-managed host keeps its https url.
    if host in GITHUB_HOSTS or ":" in host or not _glab_prefers_ssh(host):
        return url
    return f"git@{host}:{path}.git"


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
            # The redacted body goes in as text: `--description` exists in every glab
            # release, while `--description-file` is missing from older ones and makes
            # `glab mr create` exit on an unknown flag after the branch is pushed.
            "--description",
            body_path.read_text(encoding="utf-8"),
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
        """The first real MR URL in ``glab``'s stdout — see :func:`extract_mr_url`."""
        return extract_mr_url(stdout)
