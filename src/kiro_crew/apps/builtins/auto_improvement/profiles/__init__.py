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
