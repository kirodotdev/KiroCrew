"""Guard: a promoted STABLE build must DISPLAY a clean base version.

Promotion ships the soaked candidate's exact bytes, which carry a prerelease
stamp (``0.3.0rc13`` wheel / ``0.3.0-insider.13`` desktop) baked in at
insider-build time. That stamp is load-bearing on insider (distinct immutable
per-version keys) and for the auto-updater compare gate (app version == feed
version), so it cannot be made clean in the bytes without abandoning promotion.
The fix therefore lives at the DISPLAY layer: ``_display_version`` folds to the
clean base on the stable channel only. This test is the regression gate that
keeps a stable user from ever being shown an ``-rc``/``-insider`` version again.

``_display_version`` is pure (channel passed in, not read) so it never touches
the event loop; these cases assert the fold directly.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.handlers.updates import _display_version


@pytest.mark.parametrize(
    "stamped, expected",
    [
        ("0.3.0rc13", "0.3.0"),  # promoted wheel stamp
        ("0.3.0-insider.13", "0.3.0"),  # promoted desktop stamp
        ("0.4.0rc2", "0.4.0"),
        ("0.4.0-insider.2", "0.4.0"),
        ("1.2.3", "1.2.3"),  # already clean -> unchanged
    ],
)
def test_stable_folds_display_to_clean_base(stamped, expected):
    assert _display_version(stamped, "stable") == expected


@pytest.mark.parametrize("channel", ["insider", "nightly", ""])
@pytest.mark.parametrize("stamped", ["0.4.0rc2", "0.4.0-insider.2", "0.4.0-nightly.20260821"])
def test_non_stable_keeps_the_full_stamp(channel, stamped):
    # The prerelease number is meaningful off the stable channel (and an unknown
    # "" channel must not fold either) -- never touch it.
    assert _display_version(stamped, channel) == stamped
