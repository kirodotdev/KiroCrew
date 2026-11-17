"""The release lane this build was packaged for, as baked into ``_build_info.py``.

``scripts/stamp-distribution.sh`` writes ``BUILD_CHANNEL`` into the generated
``_build_info`` module when the packaging job exports ``KC_BUILD_CHANNEL``
(``nightly.yml`` passes ``nightly`` to every build workflow). It is a fact about
the BYTES, not about the install: the ``$KIROCREW_HOME/channel`` file
(:func:`kiro_crew.platform.update_layout.release_channel`) is which feed the
install follows next, and a user can switch it on a stable build before the
update lands. A default that has to describe the running build reads this.

Empty in a source checkout (no ``_build_info.py``), in a build stamped before
the field existed, and in every build whose packaging job exported no channel
(release.yml today).
"""

from __future__ import annotations

#: The lanes a stamp may name. Matches ``platform.update_layout.RELEASE_CHANNELS``
#: and the ``case`` in ``scripts/stamp-distribution.sh``.
KNOWN_BUILD_CHANNELS = frozenset({"stable", "insider", "nightly"})

BUILD_CHANNEL_NIGHTLY = "nightly"

# ``_build_info`` exists only in a packaged artifact, so ImportError is the normal
# case in a checkout. Its own import, like beacon's WEBSITE_TREE: a stamp written
# before this field existed raises ImportError for the name and must read as "".
# Held in a module binding, which is also the seam tests patch.
try:
    from ._build_info import BUILD_CHANNEL as _BAKED_BUILD_CHANNEL  # type: ignore[import-not-found]
except ImportError:
    _BAKED_BUILD_CHANNEL = ""


def baked_build_channel() -> str:
    """The lane this build was stamped for, or ``""`` when it carries none."""
    value = _BAKED_BUILD_CHANNEL
    if isinstance(value, str) and value in KNOWN_BUILD_CHANNELS:
        return value
    return ""


def is_nightly_build() -> bool:
    """Whether this build's bytes came from the nightly pipeline."""
    return baked_build_channel() == BUILD_CHANNEL_NIGHTLY
