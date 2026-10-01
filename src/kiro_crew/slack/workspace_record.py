"""The shape of the Slack workspace record, shared by its two readers.

``SLACK_WORKSPACE_STATE_FILENAME`` beside the session map holds the ``auth.test``
``team_id`` the persisted Slack destinations were written under, and -- while a
workspace switch is in flight -- a marker naming the target workspace with a
copy of the rows the switch sweeps (see ``slack.gateway._SlackWorkspaceRecord``).
The gateway reads it on the connect path; the snapshot restore validates it
before installing a bundle's copy. Both must accept exactly the same documents,
so the shape lives here, in a leaf module with no imports of its own beyond the
standard library: the snapshot facade must not pull the gateway module onto its
import path, and the gateway must not depend on the snapshot family.
"""

from __future__ import annotations

import re
from typing import Any

#: A Slack conversation id -- C (channel), D (DM), G (legacy private), W (Slack
#: Connect) followed by upper-case alphanumerics, at most 20 characters (the
#: same shape ``validation.CHANNEL_ID_RE`` / ``CHANNEL_MAX_LEN`` enforce on
#: operator input; spelled here because this module imports nothing of the
#: package). What tells a Slack DESTINATION parked in the legacy
#: ``slack_channel_id`` field from the namespaced non-Slack bucket
#: (``discord:<id>``) the dispatcher parks in the same field: the bucket
#: carries a colon and lower-case letters, which this shape never does.
SLACK_CONVERSATION_ID_RE = re.compile(r"^[CDGW][A-Z0-9]{1,19}$")

#: Key prefix of a switch-marker row that names a CRON JOB's Slack destination
#: (``channel`` / ``thread_ts`` on the job) rather than a session-map link.
#: Distinct from ``cron:`` -- a cron run's own session key in the map -- so the
#: two kinds never collide. Such a row is put back through the cron store, so
#: it is bounded by the cron store's own caps below: a row the marker retains
#: must always be one the store accepts back, or an undo would raise on it at
#: every connect with the marker never cleared and Slack never connected.
SLACK_CRON_DESTINATION_KEY_PREFIX = "cronjob:"
#: The cron store's caps on a job's Slack destination fields
#: (``cron_service.fields._CRON_STRING_FIELD_CAPS``: ``channel`` at
#: ``validation.CHANNEL_MAX_LEN``, ``thread_ts`` at 30), spelled here because
#: this module imports nothing of the package; a test pins the two together.
SLACK_CONVERSATION_ID_MAX_CHARS = 20
SLACK_THREAD_TS_MAX_CHARS = 30


def names_slack_conversation(value: object) -> bool:
    """Whether *value* is a Slack conversation id (see ``SLACK_CONVERSATION_ID_RE``)."""
    return isinstance(value, str) and SLACK_CONVERSATION_ID_RE.match(value) is not None


def is_slack_destination_row(entry: object) -> bool:
    """Whether a session-map entry names a Slack destination.

    A thread (``slack_thread_ts``) is one; so is a Slack conversation id in
    ``slack_channel_id`` with NO thread -- the flat-DM session
    (``slack.dm_single_session``) is keyed by its DM channel alone, and cron
    and unattended deliveries read that channel back (``get_channel``). Both
    were minted by one workspace and both are what a workspace switch sweeps,
    copies and refuses writes of; the namespaced non-Slack bucket in the same
    field is neither.
    """
    if not isinstance(entry, dict):
        return False
    thread_ts = entry.get("slack_thread_ts")
    if isinstance(thread_ts, str) and thread_ts:
        return True
    return names_slack_conversation(entry.get("slack_channel_id"))


#: The record's file name, beside ``session_map.json`` in the data home. Spelled
#: here so the gateway (its reader) and the snapshot family (which validates and
#: installs it, and refuses a map that arrives without it) name the same file.
SLACK_WORKSPACE_STATE_FILENAME = "slack_workspace.json"

#: The most swept rows a switch marker retains, and the longest string any of
#: their fields may hold. A row is one persisted Slack thread / channel binding
#: (key, thread ts, channel id, nonce, mute flag); the marker is read on the
#: connect path, so its size must not scale without bound with a long-lived
#: install's link count. A switch whose sweep would not fit is REFUSED before
#: anything is swept -- never retained in part. The field cap sits above the
#: longest value any in-process writer produces (a session key is accepted up
#: to 512 characters; thread ids and channel ids are far shorter), so a row the
#: map holds is never one the marker cannot hold.
SLACK_SWITCH_MARKER_MAX_ROWS = 2000
SLACK_SWITCH_MARKER_MAX_FIELD_CHARS = 1024

#: The only fields a swept row may carry -- exactly what
#: ``SessionMap.snapshot_slack_links`` writes and ``restore_slack_links`` reads.
#: A row is refused for any other field: an unknown field is unbounded by the
#: checks below (it could hold a nested container of any size), and the bound
#: on what the marker retains has to cover every field it retains.
SLACK_SWITCH_ROW_FIELDS = frozenset(
    {"key", "slack_thread_ts", "slack_channel_id", "slack_link_nonce", "slack_paused"}
)


def slack_switch_row_defect(row: object) -> str | None:
    """Why *row* is not a swept-link row the restore accepts, or None.

    The shape ``SessionMap.snapshot_slack_links`` produces and
    ``restore_slack_links`` consumes: string ``key``; string ``slack_thread_ts``
    (empty for a flat-DM row, which names its channel alone) and
    ``slack_channel_id`` a string or None, at least one of the two naming a
    destination;
    ``slack_link_nonce``, when present, a string; ``slack_paused``, when
    present, a bool; no other field; every retained value a string within
    ``SLACK_SWITCH_MARKER_MAX_FIELD_CHARS``, a bool or None. A row the restore would hand a
    consumer in the wrong type is not a row, and a row carrying a field the
    restore never reads is one whose size nothing here bounds.
    """
    if not isinstance(row, dict):
        return "row is not an object"
    unknown = sorted(name for name in row if name not in SLACK_SWITCH_ROW_FIELDS)
    if unknown:
        return f"unknown field(s) {', '.join(repr(n) for n in unknown)}"
    key = row.get("key")
    if not isinstance(key, str) or not key:
        return "'key' is not a non-empty string"
    thread_ts = row.get("slack_thread_ts")
    if thread_ts is not None and not isinstance(thread_ts, str):
        return "'slack_thread_ts' is not a string or null"
    channel = row.get("slack_channel_id")
    if channel is not None and not isinstance(channel, str):
        return "'slack_channel_id' is not a string or null"
    if not thread_ts and not names_slack_conversation(channel):
        return "row names neither a thread nor a Slack conversation"
    if "slack_link_nonce" in row and not isinstance(row["slack_link_nonce"], str):
        return "'slack_link_nonce' is not a string"
    if "slack_paused" in row and not isinstance(row["slack_paused"], bool):
        return "'slack_paused' is not a boolean"
    # Every retained value is a bounded string, a bool or None -- nothing else,
    # so no value can be a container of any size, whatever its key.
    for name, value in row.items():
        if value is None or isinstance(value, bool):
            continue
        if not isinstance(value, str):
            return f"'{name}' is not a string, a boolean or null"
        if len(value) > SLACK_SWITCH_MARKER_MAX_FIELD_CHARS:
            return f"'{name}' exceeds {SLACK_SWITCH_MARKER_MAX_FIELD_CHARS} characters"
    # A cron destination row goes back through the cron store, whose caps are
    # tighter than the marker's field cap: bound it by THOSE, so the store never
    # refuses a row the marker retained.
    if key.startswith(SLACK_CRON_DESTINATION_KEY_PREFIX):
        if thread_ts and len(thread_ts) > SLACK_THREAD_TS_MAX_CHARS:
            return f"cron 'slack_thread_ts' exceeds {SLACK_THREAD_TS_MAX_CHARS} characters"
        if channel is not None and not names_slack_conversation(channel):
            return "cron 'slack_channel_id' is not a Slack conversation id"
    return None


def slack_switch_marker_defect(target_team_id: object, rows: object) -> str | None:
    """Why a switch marker naming *target_team_id* over *rows* cannot be
    retained, or None.

    The one check three callers share: the gateway asks it of the rows it is
    ABOUT to sweep and refuses the switch before sweeping when it answers; the
    record writer asks it and fails the write rather than write a partial
    copy; the record reader and the snapshot restore ask it of a ``pending``
    member on disk. A non-empty bounded string target, a list of at most
    ``SLACK_SWITCH_MARKER_MAX_ROWS`` rows, each passing
    :func:`slack_switch_row_defect`.
    """
    if not isinstance(target_team_id, str) or not target_team_id:
        return "'team_id' is not a non-empty string"
    if len(target_team_id) > SLACK_SWITCH_MARKER_MAX_FIELD_CHARS:
        return f"'team_id' exceeds {SLACK_SWITCH_MARKER_MAX_FIELD_CHARS} characters"
    if not isinstance(rows, list):
        return "'swept' is not a list"
    if len(rows) > SLACK_SWITCH_MARKER_MAX_ROWS:
        return f"'swept' holds more than {SLACK_SWITCH_MARKER_MAX_ROWS} rows"
    for i, row in enumerate(rows):
        defect = slack_switch_row_defect(row)
        if defect is not None:
            return f"'swept[{i}]': {defect}"
    return None


def slack_workspace_record_defect(parsed: dict[str, Any]) -> str | None:
    """Why *parsed* is not a workspace record the gateway accepts, or None.

    A bounded string ``team_id``; an optional ``pending`` that is an object
    carrying a non-empty bounded string ``team_id`` and a ``swept`` list of at
    most ``SLACK_SWITCH_MARKER_MAX_ROWS`` rows each passing
    :func:`slack_switch_row_defect`; no other field at either level, since a
    field nothing reads is a field nothing bounds. The gateway's loader answers "damaged" to
    anything else and then REFUSES the Slack boot until the file is repaired or
    removed; the snapshot restore refuses to install such a file in the first
    place, by this same check.
    """
    team_id = parsed.get("team_id")
    if not isinstance(team_id, str):
        return "'team_id' is not a string"
    if len(team_id) > SLACK_SWITCH_MARKER_MAX_FIELD_CHARS:
        return f"'team_id' exceeds {SLACK_SWITCH_MARKER_MAX_FIELD_CHARS} characters"
    unknown = sorted(name for name in parsed if name not in ("team_id", "pending"))
    if unknown:
        return f"unknown field(s) {', '.join(repr(n) for n in unknown)}"
    pending = parsed.get("pending")
    if pending is None:
        return None
    if not isinstance(pending, dict):
        return "'pending' is not an object"
    unknown = sorted(name for name in pending if name not in ("team_id", "swept"))
    if unknown:
        return f"'pending' has unknown field(s) {', '.join(repr(n) for n in unknown)}"
    defect = slack_switch_marker_defect(pending.get("team_id"), pending.get("swept"))
    return None if defect is None else f"'pending.{defect[1:]}"


def session_map_slack_link_count(raw: object) -> int:
    """How many persisted Slack bindings a raw ``session_map.json`` document holds.

    Read by the snapshot restore over a bundle's map BEFORE anything is
    installed, to tell a bundle that carries Slack destinations from one that
    does not. Counts the shapes the map's loader turns into a Slack
    destination: an entry naming a thread or a Slack conversation id
    (:func:`is_slack_destination_row` -- what the sweep removes), a key
    already in the ``slack:`` namespace, and a plain-string entry (the
    pre-namespace map was Slack-only: bare thread ts -> session id, which the
    loader migrates into a Slack link). An over-count only refuses a
    restore that would need a fresh snapshot anyway; an under-count would let a
    Slack destination reach a workspace it was never written under.
    """
    if not isinstance(raw, dict):
        return 0
    count = 0
    for key, entry in raw.items():
        if isinstance(entry, str):
            count += 1
        elif isinstance(entry, dict):
            if is_slack_destination_row(entry):
                count += 1
            elif isinstance(key, str) and key.startswith("slack:"):
                count += 1
    return count
