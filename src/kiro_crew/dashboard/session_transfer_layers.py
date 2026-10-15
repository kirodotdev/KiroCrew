"""Reading the two layers a bundle carries, and writing Layer B back on arrival.

Layer A is the visible transcript, read here as the chained history of a slot. Layer B
is the kiro-cli context window (``<sid>.json`` + ``<sid>.jsonl``): resolving the session
id a slot is joined to, snapshotting and reading its files on the sending side, and on
the receiving side validating, rewriting, writing, joining and removing them again.

Owner of :mod:`kiro_crew.dashboard.session_transfer`, which is its only import path:
callers and tests reach every name here as ``session_transfer.<name>``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import kiro_sessions_dir
from kiro_crew.dashboard.session_transfer_staging import (
    LayerBEvents,
    _egress_tmp_dir,
    _rm_import_temps,
)
from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.session_transfer")


def _read_chained_history(
    state: DashboardState, session_key: str
) -> tuple[list[dict], tuple[str, ...]]:
    """Read a session's full on-disk transcript. **Blocking** — file IO + JSON.

    Split out so a caller on the event loop can push it to a thread; see
    :func:`build_transfer_bundle_async`.

    Read through the DERIVATION seam
    (:meth:`ConversationLog.derive_messages_chained_with_keys`), which validates
    the file's own privacy contract and returns the exact chained membership under
    the same lock as the rows. The callers carry those keys to their publication
    hold so an assembled bundle is refused if that membership changes before
    egress. Test doubles without the keyed seam retain their single-key behavior.
    """
    if state.conversation_log:
        keyed_reader = getattr(state.conversation_log, "derive_messages_chained_with_keys", None)
        if callable(keyed_reader):
            return keyed_reader(session_key)
        return state.conversation_log.derive_messages_chained(session_key), (session_key,)
    return [], (session_key,)


def _events_jsonl_is_loadable(events: str) -> bool:
    """Whether an inbound Layer B events blob is structurally usable as JSONL.

    **Parses only — never re-serialises.** That distinction is the whole point:
    the previous version redacted each record and wrote it back, which is exactly
    what invalidated the thinking-block signatures the conversation depends on.
    Validation reads; it does not rewrite. The caller stores the original string
    unchanged.

    A non-blank record that does not parse rejects the WHOLE blob. Installing
    malformed JSONL as the peer's resumable context makes its ``session/load``
    fail later and silently fall back to transcript replay -- while this side has
    already reported ``resume_mode: session_load``, i.e. a lie. Refusing here
    degrades honestly instead (``prefix`` -> the row reads "Sent (transcript
    only)").

    Applied on BOTH sides, and cheap enough to be: the sender catches a
    crash-truncated file before pushing megabytes through the tunnel, and the
    receiver re-checks because it must not trust the peer. Both callers keep the
    ORIGINAL string; neither writes back what this parsed.
    """
    if not events:
        return True
    # Walks the blob by index so only one record is alive at a time: a split
    # would allocate every record at once, and records inside this string are
    # invisible to the parse-memory admission.
    start = 0
    end = len(events)
    while start <= end:
        stop = events.find("\n", start)
        if stop < 0:
            stop = end
        line = events[start:stop]
        start = stop + 1
        if not line.strip():
            continue
        try:
            json.loads(line)
        except Exception:
            return False
    return True


def _resolve_layer_b_sid(sessions: Any, sm_key: str) -> str:
    """Resolve *sm_key*'s resumable sid. **MUST run on the event loop.**

    ``resumable_sid`` goes through ``SessionMap.get``, which SELF-PRUNES entries
    whose session files are gone -- a write, and therefore subject to the same
    on-loop contract as every other ``SessionMap`` access. Split out so the
    blocking file read can take the resulting sid into a worker thread without
    carrying a handle to the live map.
    """
    if sessions is None:
        return ""
    try:
        return sessions.resumable_sid(sm_key) or ""
    except Exception:
        logger.debug("session_transfer: session_map lookup failed for %s", sm_key, exc_info=True)
        return ""


def _read_layer_b(sid: str) -> dict[str, Any] | None:
    """Read Layer B (the kiro-cli context) for *sid*. **Blocking IO, thread-safe.**

    Layer A (the transcript in the bundle's ``messages``) is only the DISPLAY
    copy. Layer B is the model's actual context window plus tool/compaction
    state, stored OUTSIDE the crew home at ``kiro_sessions_dir()/<sid>.{json,jsonl}``
    and joined to a slot through ``session_map.json``. Carrying it is what makes
    an imported session RESUME with full fidelity (``session/load``) instead of
    replaying the transcript as a lossy ~8K prefix.

    Takes an already-resolved **sid**, never the live ``SessionManager``: the
    lookup that produces it (``resumable_sid`` -> ``SessionMap.get``) SELF-PRUNES
    entries whose files are gone, so it is a map *mutation* and must run on the
    event loop -- the same threading contract that governs the join
    (``subagent.py``: all ``SessionMap`` access stays on the loop because the map
    is an unlocked dict with whole-file saves). The caller resolves the sid on the
    loop and hands this function nothing but an immutable string.

    Returns ``{"sid", "envelope", "events"}`` or ``None`` when there is no Layer B
    (no sid, or the files are missing). Never raises — a transfer must degrade to
    transcript-only rather than fail.
    """
    if not sid:
        return None
    try:
        d = kiro_sessions_dir()
        jf = d / f"{sid}.json"
        lf = d / f"{sid}.jsonl"
        if not jf.exists() or not lf.exists():
            return None
        envelope = json.loads(jf.read_text(encoding="utf-8"))
        if not isinstance(envelope, dict):
            return None
        snapshot = _snapshot_events_file(lf)
    except Exception:
        logger.debug("session_transfer: could not read Layer B for sid=%s", sid, exc_info=True)
        return None
    if snapshot is None:
        # A crash-truncated source file (kiro-cli killed mid-write) would ship a
        # blob the peer must refuse. Catch it here so the copy degrades to
        # transcript-only without pushing megabytes through the tunnel first.
        # Parse-only -- see below on why nothing is rewritten.
        logger.debug("session_transfer: Layer B for sid=%s is not loadable JSONL", sid)
        return None
    # Shipped BYTE-EXACT, deliberately: no redaction pass over Layer B.
    #
    # This is not an oversight, it is forced. The envelope carries thinking
    # blocks whose ``data.signature`` is a cryptographic signature OVER the
    # thinking content, and the provider validates it when the conversation is
    # replayed. Rewriting any covered byte invalidates it, so the peer's
    # ``session/load`` succeeds and then the very next turn is rejected -- a
    # failure that surfaces far from its cause. Redacting this artifact and
    # transplanting it are mutually exclusive; measured against this machine's own
    # 704 sessions, a leaf-string redaction pass rewrote a signature in 41% of
    # them.
    #
    # What makes byte-exact acceptable is the destination, not the payload: a send
    # goes hub -> the OPERATOR'S OWN peer instance, over a tunnel that operator
    # authenticated, and the peer stores it 0600. Layer B never leaves the user's
    # own trust boundary, and copying their own context between their own machines
    # is the operation they asked for. **Layer A keeps its redaction** -- that
    # text is rendered in a transcript and re-read by an agent as context, so it
    # stays scrubbed on the same boundary.
    return {"sid": sid, "envelope": envelope, "events": LayerBEvents(snapshot)}


def _snapshot_events_file(src: Path) -> Path | None:
    """Copy the event log *src* to a private temp file, validating as it copies.

    **Blocking IO, thread-safe.** The same check :func:`_events_jsonl_is_loadable`
    makes on a string -- every non-blank record parses as JSON -- applied one
    record at a time, so the log is never resident whole: memory is bounded by
    its longest single record. The bytes are copied unchanged (see
    :func:`_read_layer_b` on why nothing is rewritten).

    Returns the snapshot's path, or ``None`` (and no file left behind) when a
    record does not parse or the log is not UTF-8.
    """
    fd, name = tempfile.mkstemp(dir=str(_egress_tmp_dir()), suffix=".jsonl")
    dst = Path(name)
    ok = False
    try:
        # The snapshot's descriptor is taken over first, so a source that
        # cannot be opened leaves nothing open behind it.
        with os.fdopen(fd, "wb") as fout, open(src, "rb") as fin:
            for line in fin:
                text = line.decode("utf-8")
                if text.strip():
                    json.loads(text)
                fout.write(line)
        ok = True
        return dst
    except Exception:
        return None
    finally:
        if not ok:
            _rm_import_temps(dst)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rewrite_layer_b_envelope(env: dict[str, Any], new_sid: str, agent: str) -> dict[str, Any]:
    """Rewrite the machine-specific fields of a Layer B ``<sid>.json`` envelope.

    The conversation itself (``session_state.conversation_metadata`` — the
    compaction/turn state that IS the resumable context) is kept verbatim. Only
    the fields that reference the SOURCE host are rewritten, because they would
    otherwise point a resumed session at paths, an id, or an agent that do not
    exist here:

    * ``session_id`` → a FRESH uuid, so copy-never-move holds — a repeat send
      cannot collide with an earlier copy on this host;
    * ``cwd`` and ``permissions.filesystem.allowed_*_paths`` → cleared, matching
      the deliberate decision to drop ``project``; the imported session is
      unscoped until the user re-picks a checkout;
    * ``agent_name`` → the target-resolved agent (or ``None``);
    * timestamps refreshed; ``title`` dropped (it lives on the Layer A slot).
    """
    e = dict(env)
    e["session_id"] = new_sid
    e["cwd"] = ""
    now = _iso_now()
    e["created_at"] = now
    e["updated_at"] = now
    e["title"] = None
    ss = dict(e.get("session_state") or {})
    ss["agent_name"] = agent or None
    perms = dict(ss.get("permissions") or {})
    fs = dict(perms.get("filesystem") or {})
    for k in ("allowed_read_paths", "allowed_write_paths"):
        if k in fs:
            fs[k] = []
    perms["filesystem"] = fs
    ss["permissions"] = perms
    e["session_state"] = ss
    return e


def _write_layer_b_files(layer_b: dict[str, Any], agent: str) -> str | None:
    """Write imported Layer B to disk under a FRESH sid. **Blocking IO, thread-safe.**

    Deliberately does NOT touch the session map. ``SessionMap.set`` mutates a
    shared ``_data`` dict and then serialises the WHOLE file, so calling it from
    a worker thread races the event loop's own map writes: two concurrent
    whole-file writes can interleave and lose an entry, which is the same
    lost-resume-mapping failure this feature exists to avoid. The join is
    therefore performed by the caller ON THE LOOP -- see
    :func:`_join_layer_b`.

    Rewrites the envelope, re-redacts the events (ingress; the sender is not
    trusted), and returns the new sid, or ``None`` on any failure so the caller
    keeps the transcript-only import.
    """
    new_sid = ""
    try:
        new_sid = str(uuid.uuid4())
        # Installed BYTE-EXACT. The envelope is rewritten only where it names the
        # SOURCE HOST (sid / cwd / paths / agent / timestamps) -- never in the
        # conversation payload, whose thinking-block signatures the provider
        # validates on replay. See _read_layer_b for why redacting and
        # transplanting cannot both hold.
        envelope = _rewrite_layer_b_envelope(layer_b.get("envelope") or {}, new_sid, agent)
        events = layer_b.get("events") or ""
        if not _events_jsonl_is_loadable(events):
            # Structural check, not a rewrite: a record the sender shipped does
            # not parse. Installing it would make this side's own
            # ``session/load`` fail later; refuse now so the import lands as the
            # transcript-only copy.
            logger.warning(
                "session_transfer: refusing unparseable Layer B from the peer; "
                "importing transcript-only"
            )
            return None
        d = kiro_sessions_dir()
        # Owner-only, because Layer B is the model's WHOLE context window -- every
        # user turn and tool result in the session -- and default umask 022 would
        # land it at 0644 for any other local user to read.
        #
        # Only the directory WE create is hardened. This is kiro-cli's own
        # sessions dir, so chmod-ing a pre-existing one would mutate posture on a
        # directory this code does not own; the files are 0600 either way, which
        # is what actually contains the content. When we do create it, the chmod
        # is separate from ``mkdir`` because mkdir's mode argument is masked by
        # the umask (``pod/runtime.py`` makes the same two-step call for the same
        # reason).
        created = not d.exists()
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        if created:
            platform_compat.chmod_safe(d, 0o700)
        for path, text in (
            (d / f"{new_sid}.json", json.dumps(envelope)),
            (d / f"{new_sid}.jsonl", events),
        ):
            # The shared helper, which every atomic-write site in the repo is
            # required to use: it allocates the temp file with ``mkstemp`` so
            # concurrent writers cannot collide on a deterministic ``.tmp`` name
            # (an ENOENT race a hand-rolled write is exposed to), and it retries
            # the Windows rename window.
            #
            # ``restrict_to_owner=True`` applies the owner-only lockdown to the
            # temp file BEFORE any content reaches it — POSIX mode bits are
            # meaningless against NTFS ACLs, and locking down only after the
            # rename leaves Layer B readable under the inherited DACL for the
            # whole write window. It implies 0o600 on POSIX, and the default
            # ``restrict_on_error="raise"`` keeps this site FAIL CLOSED.
            try:
                atomic_write(path, text, restrict_to_owner=True)
            except OSError:
                # FAIL CLOSED. This is the model's whole context window; leaving it
                # readable by other local accounts on a shared machine is worse
                # than not resuming, and this feature already has an honest
                # fallback for exactly that -- returning ``None`` imports the
                # session transcript-only. Both files go, not just this one: the
                # pair is useless alone and the ``.json`` carries context too
                # (a lockdown failure on the SECOND file leaves the first,
                # already-published one behind otherwise).
                #
                # This overrides the warn-and-continue precedent in
                # ``handlers/weixin_qr.py``. That path has no fallback -- refusing
                # breaks the feature outright -- whereas refusing here costs only
                # resume fidelity, so the same trade resolves the other way.
                #
                # The outer ``except Exception`` below performs the same
                # cleanup as a backstop for non-OSError failures; keep the two
                # paths in sync.
                logger.warning(
                    "session_transfer: could not write owner-only Layer B file %s; "
                    "discarding the pair and importing transcript-only",
                    path.name,
                    exc_info=True,
                )
                _unlink_layer_b_files(new_sid)
                return None
        return new_sid
    except Exception:
        logger.warning(
            "session_transfer: Layer B file materialisation failed; "
            "session imports transcript-only",
            exc_info=True,
        )
        # Remove whatever landed. The pair is written one file at a time, so a
        # failure on the SECOND write (disk full, EIO) leaves the first behind:
        # an orphaned half-pair that no join references, that ``_read_layer_b``
        # will not load because it requires both files, and that nothing else ever
        # cleans up. ``new_sid`` is bound before the try so this path can name it
        # even if the failure happened before the assignment inside.
        _unlink_layer_b_files(new_sid)
        return None


def _join_layer_b(sessions: Any, sm_key: str, sid: str) -> bool:
    """Point the session map at *sid*. **MUST run on the event loop.**

    Two constraints meet here:

    * the join must go through the **LIVE** map (``seed_conversation``), not a
      fresh ``SessionMap()`` -- ``SessionManager`` holds a long-lived map whose
      ``_data`` is loaded once at startup and whose every ``set`` rewrites the
      whole file from that snapshot, so a detached instance's entry is dropped
      by the next unrelated write and the tab degrades to the lossy prefix;
    * and it must run on the loop, because that same whole-file write is
      unsynchronised against concurrent session starts.

    Establishing the entry is also what auto-disables the history-prefix
    fallback, which only fires when no resumable sid exists.
    """
    if sessions is None:
        # No live manager means nothing can resume from the join anyway.
        logger.warning(
            "session_transfer: no live session manager; %s imports transcript-only", sm_key
        )
        return False
    try:
        sessions.seed_conversation(sm_key, sid, provider="acp")
        return True
    except Exception:
        logger.warning(
            "session_transfer: Layer B join failed for %s; session imports transcript-only",
            sm_key,
            exc_info=True,
        )
        return False


def _unlink_layer_b_files(sid: str) -> None:
    """Delete a materialised Layer B pair. **Blocking IO, thread-safe.**

    File-only, for the same reason :func:`_write_layer_b_files` is: the map half
    of the rollback belongs on the event loop.
    """
    if not sid:
        return
    for suffix in (".json", ".jsonl"):
        try:
            (kiro_sessions_dir() / f"{sid}{suffix}").unlink(missing_ok=True)
        except Exception:
            logger.debug("session_transfer: could not remove %s%s", sid, suffix, exc_info=True)


def _forget_layer_b_join(sessions: Any, sm_key: str) -> str:
    """Drop *sm_key*'s join and return the sid it pointed at. **On the loop.**

    Needed because the join is written BEFORE the transcript is persisted (see
    the ordering note in :func:`api_chat_slot_import`): a later failure rolls the
    slot back, so without this the map keeps an entry for a session that has no
    tab. Never raises — it runs on a path already returning a failure.
    """
    if sessions is None:
        return ""
    try:
        return sessions.forget_conversation(sm_key) or ""
    except Exception:
        logger.debug("session_transfer: could not drop the Layer B join", exc_info=True)
        return ""
