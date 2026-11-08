"""Whether the user put off a specific update, as the unattended apply reads it.

The dashboard's update prompts record the answer in ``dashboard.update_nudge``:
"Remind me tomorrow" (and any dismissal) writes a snooze for one version, "Skip
this version" a skip. The background coordinator reads the same record, so an
update the user said "later" to is never installed and restarted behind their
back. It stays available for the user to install from the prompt or Settings,
and the record names one version only, so a newer release is offered again.

"Later" never turns into a silent install when the snooze runs out: the prompt
opens again at that moment, and the deferral holds for one more prompt window
(``REPROMPT_WINDOW_SECS``) so the user can answer it. Only a prompt left
unanswered for that whole window lets the update install unattended.
"""

from __future__ import annotations

import time
from typing import Any

# How long the prompt that reopens after a snooze waits for an answer; the
# same length as the snooze itself (``SNOOZE_SECS`` in updateNudge.ts).
REPROMPT_WINDOW_SECS = 24 * 60 * 60


def user_deferred_update(config: Any, version: str, *, now: float | None = None) -> bool:
    """True when the user snoozed or skipped *version* and has not had a full
    prompt window since the snooze ran out.

    A record for another version, a malformed record and a missing one all
    answer False: a deferral is a verdict on one release only.
    """
    if not version:
        return False
    record = getattr(getattr(config, "dashboard", None), "update_nudge", None)
    if not isinstance(record, dict) or record.get("version") != version:
        return False
    if record.get("skipped") is True:
        return True
    until = record.get("snoozed_until")
    if isinstance(until, bool) or not isinstance(until, (int, float)):
        return False
    return float(until) + REPROMPT_WINDOW_SECS > (time.time() if now is None else now)
