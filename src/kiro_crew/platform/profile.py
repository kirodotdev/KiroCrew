"""Profile resolution — decides which edition loads at boot.

The profile is a *load trigger*, not a security decision: capability comes from
the installed companion package, not from the profile claim.  A forged signal at
worst loads a stricter posture on a host that has nothing to enforce it.

Precedence (first match wins):
  1. ``KIROCREW_PROFILE`` env var (explicit operator/dev override).
  2. A non-empty ``kirocrew.plugins`` entry-point group (companion installed) —
     the cheap, authoritative signal: capability comes from the installed
     companion, so its presence is what actually matters.
  3. Identity signal: a present ``~/.midway`` directory, but ONLY when the
     opt-in ``KIROCREW_MIDWAY_PROFILE_PROBE`` env var is truthy.  A cheap
     filesystem stat (no subprocess) that flags an Amazon host which has NOT
     installed the companion, so discovery fails closed instead of running open
     defaults.  The probe is OFF by default so the public open-source edition is
     never forced into the amazon profile by a stray ``~/.midway`` left behind by
     some other tool (which would otherwise brick every command at boot — there
     is no companion to compose, so discovery raises).  The Amazon companion's
     managed launcher sets ``KIROCREW_MIDWAY_PROFILE_PROBE=1`` to re-enable the
     fail-closed identity heuristic on hosts where it is meaningful.
  4. Otherwise ``standalone``.

Note: the core does NOT spawn ``kiro-cli whoami`` to read the SSO issuer — that
added a blocking subprocess to every standalone boot and baked an Amazon-only
string into the open-source core.  Entry-point presence + the opt-in
``~/.midway`` stat cover the trigger cases; the companion's own identity provider
refines the principal once loaded.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from kiro_crew.constants import env_flag_enabled
from kiro_crew.platform.context import PROFILE_AMAZON, PROFILE_STANDALONE

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)

_VALID_PROFILES = frozenset({PROFILE_STANDALONE, PROFILE_AMAZON})

# Opt-in gate for the ``~/.midway`` identity heuristic (precedence step 3).  Off
# by default so a stray ``~/.midway`` cannot force the public edition into the
# amazon profile (which has no companion to compose and would fail-closed at
# boot).  The companion's managed launcher sets this truthy.
_MIDWAY_PROBE_ENV = "KIROCREW_MIDWAY_PROFILE_PROBE"


def resolve_profile(cfg: "KiroCrewConfig", *, entry_points: "Sequence[object]") -> str:
    """Resolve the active profile.  See module docstring for precedence."""
    # 1. Explicit env override.
    env = os.environ.get("KIROCREW_PROFILE", "").strip().lower()
    if env in _VALID_PROFILES:
        return env
    if env:
        logger.warning("Unknown KIROCREW_PROFILE=%r; falling back to standalone", env)
        return PROFILE_STANDALONE

    # 2. Companion installed (cheap, authoritative — no subprocess, no marker).
    if entry_points:
        return PROFILE_AMAZON

    # 3. Identity signal — a cheap ``~/.midway`` stat (no subprocess), gated
    #    behind an explicit opt-in so the public edition is never forced into
    #    the amazon profile by a leftover ~/.midway.  Flags an Amazon host
    #    without the companion so discovery fails closed.
    if env_flag_enabled(_MIDWAY_PROBE_ENV):
        try:
            if (Path.home() / ".midway").exists():
                return PROFILE_AMAZON
        except Exception:
            logger.debug("home/.midway probe failed", exc_info=True)

    # 4. Default.
    return PROFILE_STANDALONE
