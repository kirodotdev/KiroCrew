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

#: Hosts that mean "GitHub" for provider inference. Everything else is treated as
#: GitLab — self-managed GitLab lives on arbitrary hosts, github.com does not.
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})


def _provider_of(target_url: str) -> str:
    """Infer the review provider from a target URL, defaulting to GitHub.

    Delegates to ``clone_setup.provider_for_url`` — the canonical host dispatch,
    which knows the GitLab SaaS hosts and the operator's self-managed allowlist —
    and treats its fail-closed ``""`` as GitHub: this fallback only ever fires for
    configs written before the persisted ``provider`` key existed, and those could
    only have been GitHub targets (validation refused everything else back then).
    Imported lazily: this package must stay cheap on the gateway boot path.
    """
    from ..backend.clone_setup import provider_for_url

    return provider_for_url(str(target_url or "")) or "github"


def target_provider(config: dict[str, Any] | None) -> str:
    """The configured target's review provider, from config or the target URL."""
    cfg = config or {}
    provider = str(cfg.get("provider") or "").strip().lower()
    if provider in ("github", "gitlab"):
        return provider
    return _provider_of(str(cfg.get("target_url") or ""))


def build_pr_recipe(
    config: dict[str, Any] | None,
    *,
    clone_path: Any,
    pr_queue_dir: Any,
    base_ref: str | None = None,
    fetch_url: str | None = None,
) -> Any:
    """Construct the provider-appropriate field-⑤ recipe for the configured target.

    The recipe modules are imported lazily for the same boot-cost reason as
    :func:`build_profile`. GitLab MR attribution comes from the authenticated
    ``glab`` token, so no user config key is read for that provider; ``githubUser``
    feeds only the GitHub recipe's display namespace.
    """
    cfg = config or {}
    if target_provider(cfg) == "gitlab":
        from .gitlab_repo.mr_recipe import GitLabMRRecipe

        return GitLabMRRecipe(
            user="",
            clone_path=clone_path,
            pr_queue_dir=pr_queue_dir,
            base_ref=base_ref,
            fetch_url=fetch_url,
        )
    from .github_repo.pr_recipe import GitHubPRRecipe

    return GitHubPRRecipe(
        user=str(cfg.get("githubUser") or ""),
        clone_path=clone_path,
        pr_queue_dir=pr_queue_dir,
        base_ref=base_ref,
        fetch_url=fetch_url,
    )


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
    if host in _GITHUB_HOSTS:
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
