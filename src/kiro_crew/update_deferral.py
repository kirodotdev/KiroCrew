"""Whether the user put off a specific update, as the unattended apply reads it.

The dashboard's update prompts record the answer in ``dashboard.update_nudge``:
"Remind me tomorrow" (and any dismissal) writes a snooze for one version, "Skip
this version" a skip. The background coordinator reads the same record, so an
update the user said "later" to is never installed and restarted behind their
back. It stays available for the user to install from the prompt or Settings.
A snooze names one version only, so a newer release is offered again; every
skip is kept in ``skipped_versions``, so a later snooze does not undo one.

"Later" never turns into a silent install when the snooze runs out: the prompt
opens again at that moment, and the deferral holds for one more prompt window
(``REPROMPT_WINDOW_SECS``) so the user can answer it. Only a prompt left
unanswered for that whole window lets the update install unattended.

The snooze's end is written on the browser's clock, so the dashboard's config
write also stamps the record with this host's own time (``stored_at``), and the
hold lasts until the later of the two: a browser clock behind this host cannot
cut it short.
"""

from __future__ import annotations

import time
from typing import Any

# How long "Remind me tomorrow" stays quiet (``SNOOZE_SECS`` in updateNudge.ts).
SNOOZE_SECS = 24 * 60 * 60
# How long the prompt that reopens after a snooze waits for an answer; the
# same length as the snooze itself.
REPROMPT_WINDOW_SECS = SNOOZE_SECS
# How many skips the record holds (the PATCH spec's ``max_items``). A new skip
# past the cap is refused rather than evicting one that still holds.
# shortcut: no skip is ever dropped, so the cap is reached after this many
# distinct skipped releases; add pruning that knows every update route's
# notion of "offered again" if users hit it.
MAX_SKIPPED_VERSIONS = 100


class SkipListFull(Exception):
    """A new skip would push the record past ``MAX_SKIPPED_VERSIONS``."""


def _skips_of(record: Any) -> list[str]:
    """The skips *record* holds, including a top-level skip from before the list."""
    if not isinstance(record, dict):
        return []
    raw = record.get("skipped_versions")
    skips = [v for v in raw if isinstance(v, str) and v] if isinstance(raw, list) else []
    version = record.get("version")
    if record.get("skipped") is True and isinstance(version, str) and version not in skips:
        skips.append(version)
    return skips


def with_skip_list(record: Any) -> Any:
    """*record* with its ``skipped_versions`` spelled out, as a whole-record
    writer (a change card and its Undo) reads it."""
    if not isinstance(record, dict) or "skipped_versions" in record:
        return record
    return {**record, "skipped_versions": _skips_of(record)}


def merge_update_nudge_record(saved: Any, submitted: dict) -> dict:
    """The record to store when *submitted* replaces *saved*.

    The popup sends only its verdict, with no list: the saved skips are kept in
    their order and a skip moves its version to the newest end, so a verdict
    from a dashboard holding a stale record cannot drop a skip. A new skip past
    ``MAX_SKIPPED_VERSIONS`` raises ``SkipListFull`` instead of evicting a skip
    that still holds. A record that sends the list is a whole record (a change
    card, or its Undo restoring the one it replaced) and is stored as sent.
    """
    if "skipped_versions" in submitted:
        return dict(submitted)
    skips = list(dict.fromkeys(_skips_of(saved)))
    version = submitted.get("version")
    if submitted.get("skipped") is True and isinstance(version, str) and version:
        if version not in skips and len(skips) >= MAX_SKIPPED_VERSIONS:
            raise SkipListFull(
                f"already {MAX_SKIPPED_VERSIONS} skipped versions; install an update first"
            )
        skips = [v for v in skips if v != version] + [version]
    return {**submitted, "skipped_versions": skips}


def user_deferred_update(config: Any, version: str, *, now: float | None = None) -> bool:
    """True when the user snoozed or skipped *version* and has not had a full
    prompt window since the snooze ran out.

    A record for another version, a malformed record and a missing one all
    answer False: a deferral is a verdict on one release only.
    """
    if not version:
        return False
    record = getattr(getattr(config, "dashboard", None), "update_nudge", None)
    if not isinstance(record, dict):
        return False
    if version in _skips_of(record):
        return True
    if record.get("version") != version:
        return False
    if record.get("skipped") is True:
        return True
    until = record.get("snoozed_until")
    if isinstance(until, bool) or not isinstance(until, (int, float)):
        return False
    held_until = float(until) + REPROMPT_WINDOW_SECS
    stored_at = record.get("stored_at")
    if isinstance(stored_at, (int, float)):
        held_until = max(held_until, float(stored_at) + SNOOZE_SECS + REPROMPT_WINDOW_SECS)
    return held_until > (time.time() if now is None else now)
