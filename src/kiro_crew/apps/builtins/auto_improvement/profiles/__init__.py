"""Target profiles — the plug-in seam the spine measures through.

A profile is the ONLY target-specific code in the app: it supplies the six adapters
of :class:`..spine.profile.TargetProfile` (ruler, build gate, bug runner, edit
allowlist, isolation recipe, PR recipe) plus the calibration parameters. The
dependency runs one way — a profile imports the spine, the spine never imports a
profile — so adding a target means adding a package here and nothing else.

:func:`build_profile` is the single entry point the run supervisor calls. It lives
here rather than in the profile module so the supervisor never has to know which
package implements the configured target.
"""

from __future__ import annotations

from typing import Any

__all__ = ["PROFILE_IDS", "build_profile"]

#: Selectable profile ids (config key ``profile``). ``github-repo`` is the reference
#: implementation and the default; an unknown id falls back to it rather than raising,
#: because a stale config value should not brick the Start button.
PROFILE_IDS = ("github-repo",)

#: The hosts that mean "GitHub", for the whole app (intake, transport rewrites,
#: provider dispatch). Every other host the app accepts is a GitLab.
GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})


def _provider_of(target_url: str) -> str:
    """The review provider a stored target URL names: GitHub or GitLab.

    Decided by the host alone, never by the self-managed allowlist: setup accepts
    exactly the GitHub hosts in :data:`GITHUB_HOSTS`, so every other host it stored is a
    GitLab. A GitLab target whose host later leaves ``dashboard.gitlab_hosts`` therefore
    stays a GitLab target, and its push is refused by the remote allowlist instead of
    being routed to the GitHub recipe. An empty or unparseable URL is GitHub, the forge
    every target was before GitLab support.
    """
    from urllib.parse import urlparse

    raw = str(target_url or "").strip()
    try:
        host = (urlparse(raw).hostname or "").lower().rstrip(".")
    except ValueError:
        return "github"
    if not host or host in GITHUB_HOSTS:
        return "github"
    return "gitlab"


def target_provider(config: dict[str, Any] | None) -> str:
    """The configured target's review provider. The target URL is the one source."""
    return _provider_of(str((config or {}).get("target_url") or ""))


def build_pr_recipe(
    provider: str,
    *,
    user: str,
    clone_path: Any,
    pr_queue_dir: Any,
    base_ref: str | None = None,
    fetch_url: str | None = None,
) -> Any:
    """The field-⑤ recipe for ``provider``: GitLab drafts an MR, anything else a PR.

    The one place a recipe is chosen; the profile and the draft route both call it.
    The recipe modules are imported lazily for the same boot-cost reason as
    :func:`build_profile`. ``user`` is display metadata only.
    """
    kwargs = dict(
        user=user,
        clone_path=clone_path,
        pr_queue_dir=pr_queue_dir,
        base_ref=base_ref,
        fetch_url=fetch_url,
    )
    if provider == "gitlab":
        from .gitlab_repo.mr_recipe import GitLabMRRecipe

        return GitLabMRRecipe(**kwargs)
    from .github_repo.pr_recipe import GitHubPRRecipe

    return GitHubPRRecipe(**kwargs)


def prefer_authenticated_remote(url: str) -> str:
    """Rewrite ``url`` onto the transport its host's provider CLI authenticates.

    Dispatches on the URL's own host so a caller (``backend/commit.py``) needs no
    provider knowledge. Non-HTTPS and hostless strings pass through untouched —
    the rewrites only ever change the transport of a validated HTTPS URL.
    """
    from urllib.parse import urlparse

    host = (urlparse(str(url or "").strip()).hostname or "").lower()
    if not host:
        return url
    if host in GITHUB_HOSTS:
        from .github_repo.pr_recipe import _prefer_authenticated_remote

        return _prefer_authenticated_remote(url)
    from .gitlab_repo.mr_recipe import prefer_authenticated_gitlab_remote

    return prefer_authenticated_gitlab_remote(url)


def build_profile(config: dict[str, Any]) -> Any:
    """Construct the configured :class:`~..spine.profile.TargetProfile`.

    The profile module is imported lazily inside this function on purpose:
    ``auto_improvement/__init__.py`` is deliberately a plain re-export because it runs
    on every gateway boot, and importing the profile (and through it the whole spine)
    at module scope would undo that. Nothing here is needed until a run starts.

    Raises :class:`ValueError` when no repository is configured — a user-fixable setup
    problem the supervisor turns into a 409, not a crash.
    """
    from .github_repo.profile import build_profile as _build_github_repo

    return _build_github_repo(config or {})
