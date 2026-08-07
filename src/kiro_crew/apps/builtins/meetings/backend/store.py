"""Meetings — on-disk data layout and the single path-containment barrier.

Everything lives under ``~/.kiro/crew/apps/meetings/data/`` (via
:func:`kiro_crew.apps.manager.app_data_dir`, the platform-standard app-scoped
data dir). ``root`` is accepted on every function (mirroring issue_radar's
``store.py``) so tests can point at a tmp dir instead of the real data dir.

Layout::

    <data>/config.json                       # app config (agents, providers)
    <data>/dictionary.toml                   # STT correction dictionary
    <data>/calendar-cache.json               # last calendar sync
    <data>/meetings/<safe_id>/session.json   # per-meeting metadata
    <data>/meetings/<safe_id>/tasks.json     # extracted tasks
    <data>/meetings/<safe_id>/<agent>.md     # per-agent output (markdown)
    <data>/meetings/<safe_id>/<agent>.html   # per-agent output (html widget)

Security posture (AUTOSDE ``backend-security-controls``):

* :func:`safe_meeting_id` is the ONLY way a client-supplied meeting id becomes
  a path segment. It rejects anything that is not ``[A-Za-z0-9._-]`` after the
  one documented substitution (``:`` → ``_``, because calendar event ids contain
  colons), so ``..`` and separators can never appear.
* :func:`contain` is the barrier every derived path passes through: it resolves
  (collapsing ``..`` AND following symlinks) and then asserts containment under
  the meetings data root. A violation is SEL-audited and raises. Callers MUST
  use the returned path, never the input — that is what makes a symlink planted
  inside the data dir unable to redirect a write outside it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write
from kiro_crew.sel import sel

logger = logging.getLogger("kirocrew.app.meetings")

# A meeting id is a calendar event id or a user-chosen slug. Colons are the one
# character we rewrite (calendar event ids routinely contain them, and they are
# not legal Windows path characters); everything else must already be safe.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# An agent id becomes a FILENAME stem, so it gets the same treatment.
_SAFE_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class MeetingsPathError(Exception):
    """A client-supplied path component was rejected.

    Carries an HTTP status and a machine-readable ``code``; the prose is advisory.
    """

    def __init__(self, message: str, status: int = 400, code: str = "invalid_path") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _audit(operation: str, resource: str, *, outcome: str) -> None:
    """SEL-audit a store-level decision. Never raises."""
    try:
        sel().log_api_access(
            caller=f"app:{k.APP_NAME}",
            operation=operation,
            outcome=outcome,
            resources=resource[:200],
        )
    except Exception:  # pragma: no cover — audit must never break the caller
        logger.exception("meetings: SEL audit failed for %s", operation)


# ── roots ───────────────────────────────────────────────────────────────────


def data_dir(root: Path | None = None) -> Path:
    """Return the app's data dir, creating it if missing."""
    data = root if root is not None else app_data_dir(k.APP_NAME)
    data.mkdir(parents=True, exist_ok=True)
    return data


SEED_DICTIONARY = """\
# Domain dictionary for meetings speech-to-text correction
#
# Speech recognition mangles project nouns. Add the misheard forms here once and
# every later meeting gets them right at the point of transcription -- before the
# text reaches an agent. Matching is case-insensitive, word-boundary enforced,
# and the longest alias wins.
#
# The Settings page edits this file, but hand-editing works too.

[[term]]
correct = "Kiro Crew"
aliases = ["kirocrew", "kiro crew", "kiro-crew"]

# Add your team's terms below:
# [[term]]
# correct = "DynamoDB"
# aliases = ["dynamo db", "dynamo d.b."]
"""


def ensure_data_dirs(root: Path | None = None) -> Path:
    """Create the app's data subtree and seed a starter dictionary. Idempotent.

    This is the Python home of what upstream shipped as a multi-line ``mkdir -p``
    shell blob prepended to a cron message. Nothing here overwrites an existing
    file, so a user's edits survive every restart.
    """
    data = data_dir(root)
    for name in k.DATA_SUBDIRS:
        (data / name).mkdir(parents=True, exist_ok=True)
    dictionary = data / k.DICTIONARY_FILE
    if not dictionary.exists():
        atomic_write(dictionary, SEED_DICTIONARY)
    config = config_path(root)
    if not config.exists():
        write_config(dict(DEFAULT_CONFIG), root)
    return data


def meetings_root(root: Path | None = None) -> Path:
    return data_dir(root) / "meetings"


# ── the containment barrier ─────────────────────────────────────────────────


def safe_meeting_id(raw: object) -> str:
    """Validate a client-supplied meeting id and return its filesystem form.

    Raises :class:`MeetingsPathError` for anything that is not a plain
    ``[A-Za-z0-9._-]`` token after the ``:`` → ``_`` substitution. Leading dots
    are rejected outright so an id can never name a dotfile or ``..``.
    """
    if not isinstance(raw, str):
        raise MeetingsPathError("meeting_id must be a string")
    value = raw.strip().replace(":", "_")
    if not value or len(value) > k.MAX_MEETING_ID_LEN:
        raise MeetingsPathError("meeting_id must be 1-%d characters" % k.MAX_MEETING_ID_LEN)
    if value.startswith(".") or not _SAFE_ID_RE.match(value):
        _audit("meetings.meeting_id", raw[:120], outcome="denied")
        raise MeetingsPathError("meeting_id contains illegal characters")
    return value


def safe_agent_id(raw: object) -> str:
    """Validate an agent id that will be used as an output-file stem."""
    if not isinstance(raw, str):
        raise MeetingsPathError("agent_id must be a string")
    value = raw.strip()
    if not value or len(value) > 64 or not _SAFE_AGENT_ID_RE.match(value):
        _audit("meetings.agent_id", str(raw)[:120], outcome="denied")
        raise MeetingsPathError("agent_id must be a lowercase slug")
    return value


def contain(path: Path, *, operation: str, root: Path | None = None) -> Path:
    """Resolve *path* and confirm it lands inside the meetings data root.

    The single sanitizer for every path this module builds from user input.
    ``resolve()`` collapses ``..`` and follows symlinks, then containment is
    checked; a violation is SEL-audited and raises. Callers MUST use the
    RETURNED path — that is the property which makes a symlink planted inside
    the data dir unable to redirect a read or write outside it.
    """
    base = data_dir(root).resolve()
    # ``resolve()`` of a user-derived path is the barrier itself: the very next
    # statement raises unless the result is inside ``base``. CodeQL's
    # py/path-injection query does not model the relative_to containment guard,
    # so it flags this resolve — the same false positive suppressed at
    # ``security.is_sensitive_path``'s resolve().
    resolved = path.resolve()  # lgtm[py/path-injection]
    if not resolved.is_relative_to(base):
        _audit(operation, str(path), outcome="denied")
        raise MeetingsPathError("path escapes the meetings data directory", status=403)
    return resolved


def meeting_dir(meeting_id: str, root: Path | None = None) -> Path:
    """Per-meeting folder, containment-checked."""
    safe_id = safe_meeting_id(meeting_id)
    return contain(meetings_root(root) / safe_id, operation="meetings.meeting_dir", root=root)


def agent_output_path(meeting_id: str, filename: str, root: Path | None = None) -> Path:
    """Path of one agent's output file inside a meeting folder.

    ``filename`` is always derived from a validated agent id plus a fixed
    extension (see :func:`agent_output_filename`), never taken from a request.
    It still passes through :func:`contain` — defense in depth.
    """
    return contain(
        meeting_dir(meeting_id, root) / filename,
        operation="meetings.agent_output",
        root=root,
    )


# ── generic JSON helpers ────────────────────────────────────────────────────


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("meetings: unreadable JSON at %s — using default", path)
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(payload, indent=2))


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── config ──────────────────────────────────────────────────────────────────

DEFAULT_MEETING_AGENTS: list[dict[str, Any]] = [
    {
        "id": "note-taker",
        "name": "Note Taker",
        # The agent's DECLARED name, not `meetings/meetings-note-taker`. The
        # namespaced form is a display/tracking id; the dispatchable one is the
        # `name` field from the agent JSON, which is what kiro-cli enumerates and
        # what `bridges._register_agents` publishes via
        # `publish_materialized_agents`. Asking for the namespaced form got
        # `Mode 'meetings/meetings-note-taker' not found` and no agent ever ran.
        "agent": "meetings-note-taker",
        "widget_type": "markdown",
        "enabled_by_default": True,
        "listening_by_default": True,
        "builtin": True,
    },
    {
        "id": "sketch-artist",
        "name": "Sketch Artist",
        "agent": "meetings-sketch-artist",
        "widget_type": "html",
        "enabled_by_default": True,
        "listening_by_default": True,
        "builtin": True,
    },
]

DEFAULT_CONFIG: dict[str, Any] = {
    "meeting_agents": DEFAULT_MEETING_AGENTS,
    "stt_provider": k.DEFAULT_STT_PROVIDER,
    "task_provider": k.DEFAULT_TASK_PROVIDER,
    "calendar": {"provider": k.DEFAULT_CALENDAR_PROVIDER, "source": ""},
    "presets": {},
    "default_preset": "",
    "poll_interval_active": 5000,
    "poll_interval_idle": 30000,
}


def config_path(root: Path | None = None) -> Path:
    return data_dir(root) / k.CONFIG_FILE


def _repair_builtin_agent_refs(agents: Any) -> Any:
    """Strip the stale ``meetings/`` namespace from builtin agents' ``agent``.

    Correcting :data:`DEFAULT_MEETING_AGENTS` does not reach an existing install.
    ``read_config`` merges ``{**DEFAULT_CONFIG, **raw}``, so a ``meeting_agents``
    list already in ``config.json`` wins, and the re-seed below only fires when
    that list is empty. Any user who opened Settings on a build that wrote the
    namespaced form therefore keeps it — and it is not dispatchable, so every
    meeting yields empty notes with nothing in the UI to say why, which is the
    exact failure this module's comments describe.

    Only rows flagged ``builtin`` are touched: their correct ``agent`` is known
    from :data:`DEFAULT_MEETING_AGENTS`, whereas a user-defined row's slash could
    name something we have no basis to rewrite.
    """
    if not isinstance(agents, list):
        return agents
    repaired: list[Any] = []
    for entry in agents:
        ref = entry.get("agent") if isinstance(entry, dict) else None
        if (
            isinstance(entry, dict)
            and entry.get("builtin")
            and isinstance(ref, str)
            and ref.startswith(k.LEGACY_AGENT_NAMESPACE)
        ):
            entry = {**entry, "agent": ref[len(k.LEGACY_AGENT_NAMESPACE):]}
        repaired.append(entry)
    return repaired


def read_config(root: Path | None = None) -> dict[str, Any]:
    """Read config.json, seeding defaults for a fresh install.

    Missing top-level keys are filled from :data:`DEFAULT_CONFIG` so an older
    config keeps working after an upgrade adds a field. Builtin agent references
    persisted by an older build are repaired here — see
    :func:`_repair_builtin_agent_refs`.
    """
    raw = _read_json(config_path(root), {})
    if not isinstance(raw, dict):
        raw = {}
    config = {**DEFAULT_CONFIG, **raw}
    if not config.get("meeting_agents"):
        config["meeting_agents"] = list(DEFAULT_MEETING_AGENTS)
    else:
        config["meeting_agents"] = _repair_builtin_agent_refs(config["meeting_agents"])
    if not isinstance(config.get("calendar"), dict):
        config["calendar"] = dict(DEFAULT_CONFIG["calendar"])
    return config


def write_config(config: dict[str, Any], root: Path | None = None) -> None:
    _write_json(config_path(root), config)


# ── per-meeting metadata ────────────────────────────────────────────────────


def meeting_meta_path(meeting_id: str, root: Path | None = None) -> Path:
    return contain(
        meeting_dir(meeting_id, root) / k.SESSION_META_FILE,
        operation="meetings.meta",
        root=root,
    )


#: Serializes every read-modify-write of a meeting's ``session.json``.
#:
#: Same hazard, and the same fix, as ``routes/tasks.py``'s ``_TASKS_LOCK`` — this is
#: the metadata file rather than the task list. Every mutating route reads the whole
#: dict, changes one key and writes it back, and those helpers run on worker threads
#: (``asyncio.to_thread``), so two requests genuinely execute at once. Adding an
#: attachment while another request toggles an agent meant the second write clobbered
#: the first, and BOTH reported success. ``atomic_write`` never helped: the write was
#: atomic, the read-modify-write around it was not.
#:
#: Exposed as a context manager rather than taken inside ``write_meeting_meta``,
#: because a lock held only across the write would not close the window — the read
#: has to be inside it too. One lock for all meetings: the critical section is a
#: small file read plus a write, so contention is negligible next to the bookkeeping
#: a per-id registry would need.
#: An **RLock**, not a Lock. Some helpers that own a metadata read-modify-write are
#: also called from inside another one — ``sess.start_meeting_meta`` runs under
#: ``_begin_meeting``'s transaction. With a plain Lock, making those helpers
#: self-guarding (which is what stops them racing when called DIRECTLY) would
#: deadlock the nested case instead. Re-entrancy lets every read-modify-write hold
#: the lock unconditionally, so correctness does not depend on each caller knowing
#: whether it is already inside one.
_META_LOCK = threading.RLock()


def meta_transaction() -> "threading.RLock":
    """The lock guarding a metadata read-modify-write. Use as ``with``.

    Held only across local file IO, never across an await — every caller is already
    inside one ``asyncio.to_thread`` hop.
    """
    return _META_LOCK


def read_meeting_meta(meeting_id: str, root: Path | None = None) -> dict[str, Any] | None:
    meta = _read_json(meeting_meta_path(meeting_id, root), None)
    return meta if isinstance(meta, dict) else None


def write_meeting_meta(meeting_id: str, meta: dict[str, Any], root: Path | None = None) -> None:
    _write_json(meeting_meta_path(meeting_id, root), meta)


def new_meeting_meta(meeting_id: str, title: str) -> dict[str, Any]:
    return {
        "event_id": meeting_id,
        "title": title,
        "status": k.STATUS_IDLE,
        "attachments": [],
        "outputs": {},
        "muted_agents": [],
        "created_at": utc_now_iso(),
    }


def list_meetings(root: Path | None = None) -> list[dict[str, Any]]:
    """Summaries of every meeting that has metadata on disk."""
    mroot = meetings_root(root)
    if not mroot.is_dir():
        return []
    results: list[dict[str, Any]] = []
    for meta_file in sorted(mroot.glob(f"*/{k.SESSION_META_FILE}")):
        meta = _read_json(meta_file, None)
        if not isinstance(meta, dict):
            continue
        results.append(
            {
                "event_id": meta.get("event_id", meta_file.parent.name),
                "title": meta.get("title", ""),
                "status": meta.get("status", k.STATUS_ENDED),
                "started_at": meta.get("started_at", ""),
                "ended_at": meta.get("ended_at", ""),
            }
        )
    return results


# ── tasks ───────────────────────────────────────────────────────────────────


def tasks_path(meeting_id: str, root: Path | None = None) -> Path:
    return contain(
        meeting_dir(meeting_id, root) / k.TASKS_FILE,
        operation="meetings.tasks",
        root=root,
    )


def read_tasks(meeting_id: str, root: Path | None = None) -> dict[str, Any]:
    doc = _read_json(tasks_path(meeting_id, root), None)
    if not isinstance(doc, dict):
        return {"meeting_id": meeting_id, "tasks": [], "updated_at": utc_now_iso()}
    tasks = doc.get("tasks")
    if not isinstance(tasks, list):
        doc["tasks"] = []
    return doc


def write_tasks(meeting_id: str, tasks: list[dict[str, Any]], root: Path | None = None) -> dict:
    doc = {"meeting_id": meeting_id, "tasks": tasks, "updated_at": utc_now_iso()}
    _write_json(tasks_path(meeting_id, root), doc)
    return doc


# ── agent output files ──────────────────────────────────────────────────────


def agent_output_filename(agent_def: dict[str, Any]) -> str | None:
    """Derive the output filename from an agent's id + widget_type.

    Returns None for chat-only agents (their output is the chat transcript).
    """
    ext = k.WIDGET_EXT_MAP.get(str(agent_def.get("widget_type") or k.DEFAULT_WIDGET_TYPE), ".md")
    if ext is None:
        return None
    return f"{safe_agent_id(agent_def.get('id'))}{ext}"


def ensure_agent_files(
    meeting_id: str,
    agents: list[dict[str, Any]],
    title: str = "Meeting Notes",
    root: Path | None = None,
) -> list[str]:
    """Create missing output files for *agents*. Idempotent; never overwrites."""
    created: list[str] = []
    for agent_def in agents:
        try:
            fname = agent_output_filename(agent_def)
        except MeetingsPathError:
            logger.warning("meetings: skipping agent with bad id: %r", agent_def.get("id"))
            continue
        if not fname:
            continue
        fpath = agent_output_path(meeting_id, fname, root)
        if fpath.exists():
            continue
        fpath.parent.mkdir(parents=True, exist_ok=True)
        seed = f"# {title}\n\n" if agent_def.get("widget_type") != "html" else ""
        atomic_write(fpath, seed)
        created.append(fname)
    return created


def read_agent_outputs(
    meeting_id: str, agents: list[dict[str, Any]], root: Path | None = None
) -> dict[str, str]:
    """Batch-read every configured agent's output file (missing → "")."""
    outputs: dict[str, str] = {}
    for agent_def in agents:
        try:
            fname = agent_output_filename(agent_def)
            agent_id = safe_agent_id(agent_def.get("id"))
        except MeetingsPathError:
            continue
        if not fname:
            continue
        path = agent_output_path(meeting_id, fname, root)
        try:
            outputs[agent_id] = path.read_text(encoding="utf-8") if path.is_file() else ""
        except OSError:
            outputs[agent_id] = ""
    return outputs


def write_agent_output(
    meeting_id: str, agent_def: dict[str, Any], content: str, root: Path | None = None
) -> None:
    fname = agent_output_filename(agent_def)
    if not fname:
        return
    path = agent_output_path(meeting_id, fname, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, content)


# ── calendar cache ──────────────────────────────────────────────────────────


def calendar_cache_path(root: Path | None = None) -> Path:
    return data_dir(root) / k.CALENDAR_CACHE_FILE


def read_calendar_cache(root: Path | None = None) -> list[dict[str, Any]]:
    events = _read_json(calendar_cache_path(root), [])
    return events if isinstance(events, list) else []


def write_calendar_cache(events: list[dict[str, Any]], root: Path | None = None) -> None:
    _write_json(calendar_cache_path(root), events[: k.MAX_CALENDAR_EVENTS])


def dictionary_path(root: Path | None = None) -> Path:
    return data_dir(root) / k.DICTIONARY_FILE
