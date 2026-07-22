"""Profile resolution — decides which edition loads at boot.

The profile is a *load trigger*, not a security decision: capability comes from
the installed companion package, not from the profile claim.  A forged signal at
worst loads a stricter posture on a host that has nothing to enforce it.

Precedence (first match wins):
  1. ``KIROCREW_PROFILE`` env var (explicit operator/dev override).
  2. A non-empty ``kirocrew.plugins`` entry-point group (companion installed) —
     the cheap, authoritative signal: capability comes from the installed
     companion, so its presence is what actually matters.
  3. Identity signal: a present SSO-marker directory (``KIROCREW_SSO_MARKER_PATH``,
     default ``~/.midway``), but ONLY when the opt-in ``KIROCREW_SSO_PROFILE_PROBE``
     env var (or the legacy ``KIROCREW_MIDWAY_PROFILE_PROBE``, still set by
     already-deployed launchers) is truthy.  A cheap filesystem stat (no
     subprocess) that flags an enterprise host which has NOT installed the
     companion, so discovery fails closed instead of running open defaults.  The
     probe is OFF by default so the public open-source edition is never forced
     into the enterprise profile by a stray marker directory left behind by some
     other tool (which would otherwise brick every command at boot — there is no
     companion to compose, so discovery raises).  A companion's managed launcher
     sets the probe env var to ``1`` to re-enable the fail-closed identity
     heuristic on hosts where it is meaningful.
  4. Otherwise ``standalone``.

Note: the core does NOT spawn a subprocess to read the SSO issuer — that added a
blocking subprocess to every standalone boot and baked an edition-specific string
into the open-source core.  Entry-point presence + the opt-in marker stat cover
the trigger cases; the companion's own identity provider refines the principal
once loaded.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from kiro_crew.constants import env_flag_enabled
from kiro_crew.platform.context import PROFILE_ENTERPRISE, PROFILE_STANDALONE

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)

_VALID_PROFILES = frozenset({PROFILE_STANDALONE, PROFILE_ENTERPRISE})

# Back-compat aliases for the ``KIROCREW_PROFILE`` env value: an operator or a
# companion launcher that still sets a legacy edition name resolves to the
# current profile constant rather than falling back to standalone.
_PROFILE_ALIASES = {"amazon": PROFILE_ENTERPRISE}

# Opt-in gate for the SSO-marker identity heuristic (precedence step 3).  Off by
# default so a stray marker directory cannot force the public edition into the
# enterprise profile (which has no companion to compose and would fail-closed at
# boot).  A companion's managed launcher sets this truthy.
#
# Two names are honored: the current ``KIROCREW_SSO_PROFILE_PROBE`` and the
# legacy ``KIROCREW_MIDWAY_PROFILE_PROBE`` still set by already-deployed managed
# launchers.  Dropping the legacy name would silently disable the fail-closed
# marker check on those hosts — an enterprise host with the identity marker but
# a missing/broken companion would resolve ``standalone`` and boot WITHOUT the
# security overlay instead of aborting.  Keep both until every launcher migrates.
_SSO_PROBE_ENV = "KIROCREW_SSO_PROFILE_PROBE"
_LEGACY_SSO_PROBE_ENV = "KIROCREW_MIDWAY_PROFILE_PROBE"

# Env var overriding the path stat'd for the identity heuristic when the probe
# is opted in, so a companion can point it at its own SSO marker.  The default
# (the historic location, for hosts that already rely on it) is resolved at call
# time in ``resolve_profile`` so ``Path.home`` stays monkeypatchable in tests.
_SSO_MARKER_ENV = "KIROCREW_SSO_MARKER_PATH"
_DEFAULT_SSO_MARKER_NAME = ".midway"


def resolve_profile(cfg: "KiroCrewConfig", *, entry_points: "Sequence[object]") -> str:
    """Resolve the active profile.  See module docstring for precedence."""
    # 1. Explicit env override.
    env = os.environ.get("KIROCREW_PROFILE", "").strip().lower()
    if env in _VALID_PROFILES:
        return env
    if env in _PROFILE_ALIASES:
        return _PROFILE_ALIASES[env]
    if env:
        logger.warning("Unknown KIROCREW_PROFILE=%r; falling back to standalone", env)
        return PROFILE_STANDALONE

    # 2. Companion installed (cheap, authoritative — no subprocess, no marker).
    if entry_points:
        return PROFILE_ENTERPRISE

    # 3. Identity signal — a cheap marker stat (no subprocess), gated behind an
    #    explicit opt-in so the public edition is never forced into the
    #    enterprise profile by a leftover marker.  Flags an enterprise host
    #    without the companion so discovery fails closed.
    if env_flag_enabled(_SSO_PROBE_ENV) or env_flag_enabled(_LEGACY_SSO_PROBE_ENV):
        marker = os.environ.get(_SSO_MARKER_ENV, "").strip()
        marker_path = Path(marker) if marker else (Path.home() / _DEFAULT_SSO_MARKER_NAME)
        try:
            if marker_path.exists():
                return PROFILE_ENTERPRISE
        except Exception:
            logger.debug("SSO-marker probe failed", exc_info=True)

    # 4. Default.
    return PROFILE_STANDALONE
