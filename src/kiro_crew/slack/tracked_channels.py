"""Tracked Slack channels: how the owner adds one, and the edit the dashboard saves.

``slack.tracking_channels`` is the list of channels the agent may post to, react
in, pin in and read. Two owner-only routes change it, and both apply at once:
the Slack Settings page in the dashboard (local machine only) and Slack's
``/kirocrew channels`` modal. A refusal for an untracked channel names them here,
so the agent repeats the real route instead of guessing one.
"""

from __future__ import annotations

import re

#: A public (``C``) or private (``G``) channel ID: the kinds a tracked channel is.
TRACKED_CHANNEL_ID_RE = re.compile(r"[CG][A-Z0-9]{2,}")

#: The longest stored ID a remove may name. A remove matches the stored ID
#: verbatim: the loader also keeps entries an add refuses (a hand-written ``D…``
#: ID), and the owner must be able to drop exactly the one they picked.
STORED_CHANNEL_ID_MAX = 128

#: Where the owner tracks a channel. The dashboard path is the Settings page's own
#: labels (``SlackPanel.tsx``); the agent is told that only the owner can do it, so
#: a shared user is pointed at the owner rather than at a route that refuses them.
TRACK_ROUTE = (
    "Only the Kiro Crew owner can track a channel: in the dashboard, Settings → "
    "Messaging Channels → Slack → Tracked channels, then Save; or in Slack, "
    "/kirocrew channels → Add a channel. Either applies at once, with no restart."
)


def untracked_channel_error(channel: str) -> str:
    """The refusal for *channel* not being tracked, with the route to track it."""
    return f"channel {channel} not in tracked channels. {TRACK_ROUTE}"


class TrackingListMalformed(ValueError):
    """``slack.tracking_channels`` in config.json is not a list, so no edit applies."""


def _entry_id(entry: object) -> str:
    """The channel ID of a stored entry: an object's ``channel_id`` or a bare string."""
    if isinstance(entry, dict):
        # As GET lists it (``str(channel_id)``), so a remove meets the same text.
        cid = entry.get("channel_id")
        return str(cid) if cid else ""
    return entry if isinstance(entry, str) else ""


def apply_tracking_edit(section: dict, adds: list[str], removes: list[str]) -> bool:
    """Add and remove channel IDs in ``section["tracking_channels"]``, in place.

    Applied to the section as re-read inside the config lock, so a channel added
    in Slack since the page loaded stays. A kept entry keeps its own fields
    (activation, agent, name); an added one is ``{"channel_id": ...}``, which reads
    as the default ``mention`` activation. A remove wins over an add of the same
    ID. Returns whether the list changed. Raises :class:`TrackingListMalformed`
    when the stored value is neither absent nor a list.
    """
    stored = section.get("tracking_channels")
    if stored is None:
        stored = []
    if not isinstance(stored, list):
        raise TrackingListMalformed("slack.tracking_channels is not a list")
    drop = set(removes)
    kept = [e for e in stored if _entry_id(e) not in drop]
    have = {_entry_id(e) for e in kept}
    for cid in adds:
        if cid not in drop and cid not in have:
            kept.append({"channel_id": cid})
            have.add(cid)
    if kept == stored:
        return False
    section["tracking_channels"] = kept
    return True
