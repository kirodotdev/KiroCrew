#!/usr/bin/env python3
"""Follow-up sessions — ask the reviewer about its findings later, from disk.

A review's reasoning lives in its session context, not in its report. The report
carries the *conclusions* (observation / consequence / suggestion); "why did you
decide that?" is answerable only by the session that decided it.

So the reviewer's session is not kept RESIDENT — it is kept RESUMABLE:

  * ``ReviewPool.send(..., keep_session_key=...)`` sets ``keep_transcript`` on the
    handle before tearing it down, so ``destroy()`` still terminates the session
    on kiro-cli (RSS is reclaimed immediately, as for any other review) but skips
    the transcript unlink.
  * This module records a small DESCRIPTOR beside the run naming that transcript.
  * Asking a follow-up creates an ordinary dashboard chat slot whose session id is
    the review's, so the dashboard's own resume path issues ``session/load``
    against that transcript and the whole reviewer context comes back.

Asking is the rare case — most reports are read without questions — so nothing is
held hot on the chance that it happens. A follow-up pays a cold ``session/load``
instead, and from the first turn on it is an ordinary session: it persists across
restarts, appears in the sidebar, and its tool use runs through the dashboard's
own approval pipeline rather than an app-local gate.

Two failure modes are made loud rather than silent, both in the same direction —
a resumed session that did NOT restore the review is worse than no session at
all, because it answers confidently with no idea what was reviewed:

  * The transcript must still be on disk. ``resumable`` says so before a slot is
    offered, and the run's descriptor is useless without it.
  * The resume must actually land. A slot created for a follow-up carries no
    Kiro Crew conversation log, so the dashboard's fallback (history replay) has
    nothing to replay.

Nothing here ever deletes a kiro-cli transcript. The only session id available is
the one in the descriptor, and the reviewer has shell plus a predictable path to
that file, so an id read back from it proves the FORM of a session id and never
which session Sage recorded -- deleting on that authority would let a
prompt-injected review name any session on the machine and have this app remove
it. Retiring an offer therefore removes only the descriptor; reclaiming the
transcript is the platform's own user-controlled session cleanup.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

try:
    from kiro_crew.acp.types import PROVIDER_LABEL_DEFAULT
except ImportError:  # pragma: no cover - standalone / test fallback
    PROVIDER_LABEL_DEFAULT = "acp"  # type: ignore[assignment]

try:
    from kiro_crew.config.paths import kiro_sessions_dir
except ImportError:  # pragma: no cover - standalone / test fallback
    # No fallback path. Guessing where kiro-cli keeps transcripts is how an
    # instance ends up writing them to one place and reading them from another,
    # so without the platform's resolver this module simply cannot locate one.
    kiro_sessions_dir = None  # type: ignore[assignment]

# Module scope, per the repo's top-level-imports rule. No cycle: `store` and
# `results` do not import this module, and `review_pool` (which does) imports it
# lazily inside its own functions where needed.
from sage_lib import results, store  # noqa: E402

logger = logging.getLogger(__name__)

#: An ancestor of the run's ``chat/`` directory is a link, so writing would land
#: outside the run.
ERR_LINKED_DIR = "chat_transcript_dir_unsafe"

#: The run this follow-up belongs to no longer exists.
ERR_RUN_GONE = "chat_run_deleted"

#: No descriptor was recorded for this review (it predates the feature, or its
#: review turn ended abnormally and was never kept).
ERR_NO_DESCRIPTOR = "followup_not_recorded"

#: A review whose run has not finished. Its findings are still moving, and a
#: second coverage pass may retire the descriptor the panel is looking at.
ERR_RUN_LIVE = "followup_run_live"

#: The descriptor is there but the transcript it names is gone, so there is
#: nothing to load and a slot would answer without the review.
ERR_TRANSCRIPT_GONE = "followup_transcript_gone"

#: How long a follow-up offer survives without the conversation behind it being
#: touched. Retiring the offer is all this bound does -- the transcript itself is
#: the platform's to reclaim, so a stale descriptor cannot strand disk.
OFFER_MAX_IDLE_SECS = 14 * 24 * 3600.0

#: Slot title cap. The sidebar truncates anyway; this bounds what is persisted.
TITLE_MAX = 120

#: The folder every follow-up session is filed under.
FOLDER_NAME = "Sage Review"

#: kiro-cli session ids are opaque, and one lands in a filename. Anything outside
#: this shape is refused rather than sanitized: a "cleaned" id would name a
#: DIFFERENT session's transcript.
_SID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def chat_key(run_id: str, change_id: str) -> str:
    """Identity of one follow-up: the review that produced the findings.

    Scoped to (run, change) rather than change alone because re-reviewing a pull
    request produces different reasoning, and a follow-up must belong to the
    report the user is actually looking at.
    """
    return f"{run_id}:{change_id}"


def slot_key(run_id: str, change_id: str) -> str:
    """Chat-slot key for this review's follow-up.

    Derived rather than minted so a second click reopens the existing session
    instead of creating a rival one, and short-hashed because the key becomes part
    of a session key and a persisted filename.
    """
    digest = hashlib.sha256(chat_key(run_id, change_id).encode()).hexdigest()
    return f"sage-followup-{digest[:12]}"


def slot_title(change_id: str, change_title: str = "") -> str:
    """Sidebar title for a follow-up session.

    Names the pull request rather than the run so the session is recognizable a
    week later, next to unrelated chats. A change id with no trailing number
    (a code review rather than a pull request) is used whole.
    """
    number = ""
    tail = (change_id or "").rsplit("-", 1)
    if len(tail) == 2 and tail[1].isdigit():
        number = tail[1]
    subject = f"pr#{number}" if number else (change_id or "review")
    title = f"followup-{subject}"
    if change_title.strip():
        title = f"{title}-{change_title.strip()}"
    return title[:TITLE_MAX]


def followup_dir(run_id: str, root: "Path | None" = None) -> "Path":
    """The run's ``chat/`` directory, refusing ANY linked ancestor.

    Guarding the descriptor FILE against symlinks is not enough: the reviewer has
    shell and these paths are predictable, so it can plant a link at ``chat``, or
    at the RUN directory holding it. ``mkdir(exist_ok=True)`` followed by
    ``mkstemp(dir=...)`` would then create and replace a file outside the app's
    data tree entirely.

    Containment is anchored at the RUNS ROOT, not at each component's own parent.
    Checking ``chat`` against ``run`` is vacuous when ``run`` is itself the link —
    both sides move together and the comparison passes while everything is
    outside.

    Order matters: the anchor is checked for being a link BEFORE its path is
    resolved. Resolving first is what makes containment attacker-relative — a link
    planted at ``runs`` is followed by ``resolve()``, the anchor moves to the
    attacker's directory, and every child underneath then compares as legitimately
    "inside" it. Once the chain is known to be link-free the lexical path IS the
    real path, which is what makes the resolved comparison meaningful rather than
    circular.
    """
    runs = store.runs_root(root)
    run = store.run_dir(run_id, root)
    d = run / "chat"
    # The runs root is FIRST, and unresolved: a link here would otherwise carry
    # the anchor with it.
    for part in (runs, run, d):
        if part.is_symlink():
            raise FileNotFoundError(ERR_LINKED_DIR)
    try:
        anchor = runs.resolve()
    except (OSError, ValueError):  # pragma: no cover - defensive
        raise FileNotFoundError(ERR_LINKED_DIR) from None
    for part in (run, d):
        if not part.exists():
            continue
        try:
            inside = part.resolve().is_relative_to(anchor)
        except (OSError, ValueError):  # pragma: no cover - defensive
            raise FileNotFoundError(ERR_LINKED_DIR) from None
        if not inside:
            raise FileNotFoundError(ERR_LINKED_DIR)
    return d


def descriptor_path(run_id: str, change_id: str,
                    root: "Path | None" = None) -> "Path":
    """Where one review's resume descriptor lives.

    ``change_id`` is routed through ``results.safe_change_id`` because it arrives
    from a request and lands in a filename. The suffix keeps it out of the way of
    the transcripts in the same directory.
    """
    safe = results.safe_change_id(change_id)
    return followup_dir(run_id, root) / f"{safe}.resume.json"


def transcript_path(run_id: str, change_id: str,
                    root: "Path | None" = None) -> "Path":
    """Where a follow-up's own question history lived when the app stored it.

    Read-only: history is now the chat session's, and these files are rendered so
    a review discussed before that stays readable.
    """
    safe = results.safe_change_id(change_id)
    return followup_dir(run_id, root) / f"{safe}.json"


def session_file(sid: str) -> "Path | None":
    """kiro-cli's transcript for *sid*, or None when the id is not usable.

    The id reaches here from a file the reviewer itself can write, so it is
    validated against a shape AND confined to the sessions directory. A crafted id
    would otherwise name a path outside it, which every caller then reads, deletes
    or hands to ``session/load``.
    """
    if kiro_sessions_dir is None:  # pragma: no cover - standalone fallback
        return None
    if not _SID_RE.match(sid or ""):
        return None
    sessions = kiro_sessions_dir()
    try:
        base = sessions.resolve()
        target = (sessions / f"{sid}.json").resolve()
    except (OSError, ValueError):  # pragma: no cover - defensive
        return None
    if target.parent != base:
        return None
    return target


def write_descriptor(run_id: str, change_id: str, *, sid: str, agent: str = "",
                     cwd: str = "", provider: str = PROVIDER_LABEL_DEFAULT,
                     root: "Path | None" = None) -> bool:
    """Record what a follow-up needs to resume this review. False when refused.

    ``provider`` and ``cwd`` are recorded, not inferred later: the dashboard
    discards a session id whose stored provider disagrees with the one that would
    resume it, and a resumed session needs the working directory the reviewer's
    own relative paths were written against.
    """
    if session_file(sid) is None:
        logger.warning("followup: refusing descriptor with unusable session id")
        return False
    try:
        path = descriptor_path(run_id, change_id, root)
    except FileNotFoundError:
        logger.warning("followup: descriptor path is unsafe; not recording")
        return False
    # Do NOT create the run dir: a review whose run was deleted mid-flight must
    # not resurrect the directory that deletion just removed.
    if not path.parent.parent.is_dir():
        return False
    payload = {
        "sid": sid,
        "agent": agent,
        "cwd": cwd,
        "provider": provider or PROVIDER_LABEL_DEFAULT,
        "created_at": time.time(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # mkstemp, not a predictable `<name>.tmp`: the reviewer can pre-plant a
        # symlink at a name it can guess, and writing through it would land this
        # content on whatever it points at. An O_EXCL temp with a random name
        # cannot be pre-empted, and os.replace does not follow a link at the
        # destination.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".resume-", suffix=".json")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False))
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    except Exception:
        logger.warning("followup: could not record descriptor", exc_info=True)
        return False
    return True


def read_descriptor(run_id: str, change_id: str,
                    root: "Path | None" = None) -> dict | None:
    """The recorded descriptor, or None when there is none worth trusting.

    Read through the app's no-follow chokepoint and re-validated field by field:
    the reviewer has shell and this path is predictable, so the file may be its
    own writing rather than ours.
    """
    try:
        path = descriptor_path(run_id, change_id, root)
    except FileNotFoundError:
        return None
    if not path.exists():
        return None
    try:
        raw = store.read_text_nolink(path, store.run_dir(run_id, root))
    except Exception:  # pragma: no cover - defensive
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    sid = data.get("sid")
    if not isinstance(sid, str) or session_file(sid) is None:
        return None

    def _str(value: object) -> str:
        return value if isinstance(value, str) else ""

    created = data.get("created_at")
    return {
        "sid": sid,
        "agent": _str(data.get("agent")),
        "cwd": _str(data.get("cwd")),
        "provider": _str(data.get("provider")) or PROVIDER_LABEL_DEFAULT,
        "created_at": float(created) if isinstance(created, (int, float)) else 0.0,
    }


def resumable(run_id: str, change_id: str,
              root: "Path | None" = None) -> tuple[dict | None, str]:
    """The descriptor plus "" when a follow-up can be started, else the reason.

    The transcript's presence is checked HERE rather than left to the load: a
    slot offered for a transcript that is gone resumes nothing and then answers
    anyway, which is the one outcome worse than refusing.
    """
    desc = read_descriptor(run_id, change_id, root)
    if desc is None:
        return None, ERR_NO_DESCRIPTOR
    path = session_file(desc["sid"])
    if path is None or not path.exists():
        return None, ERR_TRANSCRIPT_GONE
    return desc, ""


def forget(run_id: str, change_id: str, root: "Path | None" = None) -> None:
    """Stop offering a follow-up for this review, by dropping its descriptor.

    Called when a review is superseded within its own run, so the panel does not
    offer a session whose findings the run has since replaced.

    It deliberately does NOT unlink the kiro-cli transcript. The only session id
    available here comes from the descriptor, and the reviewer has shell plus a
    predictable path to that file, so an id read back from it proves the FORM of a
    session id and never which session Sage recorded. Deleting on that authority
    would let a prompt-injected review name any session on the machine -- a live
    dashboard chat included -- and have this app remove it. Transcript removal is
    the platform's own user-controlled cleanup; this app only ever stops pointing
    at one.
    """
    try:
        descriptor_path(run_id, change_id, root).unlink(missing_ok=True)
    except (OSError, FileNotFoundError):  # pragma: no cover - defensive
        logger.debug("followup: could not remove descriptor", exc_info=True)


def _idle_secs(sid: str, created_at: float, now: float) -> float:
    """How long this review's conversation has been untouched.

    Measured from the transcript's own mtime rather than the descriptor's write
    time, because a follow-up session RESUMES this file and keeps appending to it:
    aging on the review date alone would retire the panel's offer while the
    conversation behind it is still in use.
    """
    path = session_file(sid)
    newest = created_at
    if path is not None:
        for candidate in (path, path.with_suffix(".jsonl")):
            try:
                newest = max(newest, candidate.stat().st_mtime)
            except OSError:
                continue
    return now - newest


def prune(max_idle_secs: float = OFFER_MAX_IDLE_SECS,
          root: "Path | None" = None) -> int:
    """Retire follow-up offers nobody has touched. Returns how many were dropped.

    Only the descriptor is removed, for the same reason ``forget`` does not unlink:
    a session id read from a worker-writable file cannot authorize deleting a
    session. That also means an open follow-up conversation can never be hollowed
    out by this sweep -- the worst it can do is stop the review panel from linking
    to a conversation nobody has used in weeks.
    """
    now = time.time()
    dropped = 0
    for run_id in store.list_run_ids(root):
        try:
            d = followup_dir(run_id, root)
        except FileNotFoundError:
            continue
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.resume.json")):
            raw = store.read_text_nolink(path, store.run_dir(run_id, root))
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            sid = data.get("sid")
            if not isinstance(sid, str) or session_file(sid) is None:
                continue
            created = data.get("created_at")
            created_at = float(created) if isinstance(created, (int, float)) else 0.0
            if _idle_secs(sid, created_at, now) < max_idle_secs:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - defensive
                logger.debug("followup: could not remove an idle descriptor",
                             exc_info=True)
                continue
            dropped += 1
    return dropped


# --- legacy question history -------------------------------------------------
# Read-only. A follow-up's questions now live in its chat session, but a review
# discussed before that has its exchanges in the run's own ``chat/`` directory,
# and the panel still renders them.

_LEGACY_ROLES = ("user", "reviewer")


def _coerce_turn(item: object) -> dict | None:
    """Normalize one stored turn into the shape the panel renders, or reject it.

    Every string here is model-written or model-influenced, and the reviewer can
    write this file itself, so each one is re-scrubbed rather than trusted. The
    role is restricted to the two values the UI renders: a planted role must not
    reach a branch nobody designed.
    """
    if not isinstance(item, dict):
        return None
    role = item.get("role")
    if role not in _LEGACY_ROLES:
        return None

    def _scrub(text: object) -> str:
        if not isinstance(text, str):
            return ""
        try:
            return store.redact_text(text)
        except Exception:  # pragma: no cover - defensive
            # Fail CLOSED: an unscrubbable string is dropped rather than emitted.
            logger.debug("followup redaction failed", exc_info=True)
            return ""

    def _scrub_all(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [_scrub(v) for v in value if isinstance(v, str)]

    raw_ts = item.get("ts")
    return {
        "role": str(role),
        "text": _scrub(item.get("text")),
        "thinking": _scrub(item.get("thinking")),
        "tools": _scrub_all(item.get("tools")),
        "refusals": _scrub_all(item.get("refusals")),
        "ts": float(raw_ts) if isinstance(raw_ts, (int, float)) else 0.0,
    }


def read_transcript(run_id: str, change_id: str,
                    root: "Path | None" = None) -> list[dict]:
    """Stored question history for a review, or ``[]`` when there is none.

    Tolerant by design: a missing, unreadable or malformed file reads as "no
    history" so the panel still renders. Read through the app's no-follow
    chokepoint, which confines the resolved inode to the run directory and caps
    size — a plain read would follow a link planted at this predictable path and
    copy an arbitrary file into a transcript the dashboard renders.
    """
    try:
        path = transcript_path(run_id, change_id, root)
    except FileNotFoundError:
        return []
    try:
        raw = store.read_text_nolink(path, store.run_dir(run_id, root))
    except Exception:
        return []
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    return [t for t in (_coerce_turn(i) for i in items) if t is not None]
