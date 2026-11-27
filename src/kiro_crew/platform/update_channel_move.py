"""Which channel this install came from, once a person switched its channel file.

``$KIROCREW_HOME/channel`` names the channel the update check follows, and a switch
in Settings > About rewrites it without installing anything. After a switch the file
names the channel the check follows, not the one the running install came from, and
a restart keeps no memory of the switch. The one-click update needs exactly that fact: a newer release of the
channel the install CAME from installs with one click, and a channel move keeps the
host approval (decision entry "a same-channel signed update installs with one
click").

So the switch records the channel it moved away from, here, before it writes the
new one, and the record stays until an approved install from the followed channel
is promoted. A re-run of the installer for the chosen channel leaves it in place,
which costs one host approval more, never one less. A channel file rewritten by
anything other than that switch leaves no record; the record sits in the same data
home as the channel file, so it is a record of the dashboard's switch, not a
defence against a writer of that directory.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

_FILENAME = "channel-moved-from"

#: Serializes every change to the record together with the channel file write it
#: belongs to, so two switches, or a switch and a settlement, cannot interleave a
#: removal with a channel write and leave a move with no record.
_LOCK = threading.Lock()


def _path() -> Path:
    return data_home() / _FILENAME


def moved_from() -> str:
    """The channel this install came from, or ``""`` while it follows that channel.

    An unreadable record reads as a move: the answer that keeps the host approval.
    """
    try:
        return _path().read_text(encoding="utf-8").strip() or "unknown"
    except FileNotFoundError:
        return ""
    except OSError:
        logger.warning("Channel move record unreadable; treating it as a move", exc_info=True)
        return "unknown"


def switch_channel(requested: str, write_channel: Callable[[str], str]) -> str:
    """Switch the followed channel to *requested* through *write_channel*, keeping the record.

    The channel followed until now is the one the switch moves away from. The
    first switch away from the install's channel is recorded BEFORE the channel
    file is written, and a later switch does not overwrite it. A switch back to
    that channel removes the record only AFTER the channel file names it. Either
    way a failure leaves the record in place: a failed record write raises
    ``OSError`` before anything else is written, so the caller refuses the switch.
    Returns what *write_channel* stored. Sync: reads and writes the data home.
    """
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.platform.update_layout import release_channel

    with _LOCK:
        origin = moved_from() or release_channel()
        if requested != origin and not _path().exists():
            atomic_write(_path(), f"{origin}\n")
        stored = write_channel(requested)
        if stored == origin:
            _clear()
        return stored


def settle(installed_channel: str) -> None:
    """Record that an approved install from *installed_channel* was promoted.

    The install now came from *installed_channel*. While that is the followed
    channel there is no move, so the record goes. When the followed channel was
    switched while the install built, the install came from *installed_channel*
    and the switch is a move away from it, so that is what the record names.
    """
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.platform.update_layout import release_channel

    with _LOCK:
        if installed_channel == release_channel():
            _clear()
        else:
            atomic_write(_path(), f"{installed_channel}\n")


def settle_followed() -> None:
    """Settle after an install that took the followed channel's release (the boot apply)."""
    from kiro_crew.platform.update_layout import release_channel

    settle(release_channel())


def _clear() -> None:
    try:
        _path().unlink()
    except FileNotFoundError:
        return


__all__ = ["moved_from", "settle", "settle_followed", "switch_channel"]
