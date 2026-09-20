"""Session transfer — copy a session between Kiro Crew instances.

Two halves live here:

* :func:`build_transfer_bundle_async` serialises one slot's visible conversation
  into a portable, version-tagged dict. Called on the **sending** side.
* :func:`api_chat_slot_import` accepts such a dict and materialises it as a new
  slot. Called on the **receiving** side.

The wire hop between them is an ordinary authenticated dashboard request over
an Instances tunnel; see [instances.md](../../../docs/system-specs/modules/instances.md) §14.

``session_export`` is the third consumer: it streams the SAME bundle to a file
so the two machines need not be online at the same time. It adds no format.
``bundle_version`` stays 2 and the ``source`` record below is additive, because
:func:`_validate_bundle` refuses an unrecognised version outright while silently
dropping keys it does not know: a version bump would stop an instance that has
not updated from receiving anything, where a new optional key costs it nothing.
Nothing in this module ever requires a field to be present.

**``source`` is recorded, never applied.** It carries what the conversation ran
under — model, reasoning effort, tool-approval policy, workspace, project, plus
the export instant and the producing gateway's version — for a HUMAN reading the
file to judge what they are looking at. No import path reads it, and
``approval_policy`` in particular is never applied: ``"auto"`` means auto-approve
every tool, so applying a recorded copy would let a session arrive on another
machine pre-authorised to run tools without prompting. An imported session always
lands interactive.

**Two layers travel.** *Layer A* is the visible transcript (the bundle's
``messages``) — what the imported tab DISPLAYS. *Layer B* (bundle_version 2) is
the kiro-cli context window itself (``<sid>.json`` + ``<sid>.jsonl``, stored
outside the crew home and joined via ``session_map.json``): carrying it lets the
imported session RESUME with full fidelity through ``session/load`` rather than
replaying the transcript as a lossy ~8K prefix. Layer B is optional — a v1
sender, or a session that never opened a kiro-cli context, ships Layer A only
and the peer falls back to the prefix. Sub-agent conversations do NOT travel;
their results already live inside Layer B as injected context.

**Copy, never move.** Import always allocates a NEW slot key and never touches
an existing session, so a transfer leaves the source intact and can be repeated
safely. Nothing here deletes anything.

**What deliberately does NOT travel.** A session's transcript is portable text,
but most of its *metadata* is a reference into the local instance's object graph
— a project path, a folder id, a workspace's memory, an agent template, a bound
artifact. Carrying those across would produce dangling references that render
as broken UI on arrival, so the bundle carries the transcript, the title, and an
agent *hint* only:

* ``project`` is intentionally dropped. The source's checkout path almost never
  exists on the target host (a Mac worktree path on a Linux dev desk), and a
  slot pointing at a missing directory scopes file search and steering to
  nothing. The imported session arrives with no project so the user re-picks it.
* ``model`` is not carried. Accounts differ in entitlement, so a model id that
  the source account is served can fail at runtime on the target; the target
  resolves its own default instead (see
  docs/system-specs/common/model-selection.md).
* ``workspace`` is not carried. Workspaces are per-instance memory scopes, and a
  name that matches on both hosts still means two different memories.
* ``agent`` is carried as a hint and applied ONLY if the target has an agent by
  that name; otherwise it is dropped rather than left dangling.
* ``folder_id``, ``tags``, ``pinned``, ``artifact``, ``app``,
  ``linked_session_key`` and ``forked_from`` are all local-graph references and
  are not carried at all. The arriving session's PLACEMENT is nonetheless not the
  top level: it is derived locally from ``origin`` by
  :mod:`kiro_crew.dashboard.arrival_folders`, which files it under
  ``Imported`` / ``from <sender>``. That is the opposite of carrying the sender's
  ``folder_id`` — no id crosses the wire, and the folder is one on THIS instance.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import platform
import uuid
import zlib
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from kiro_crew import __version__, platform_compat
from kiro_crew.agent_discovery import list_agents
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import kiro_sessions_dir

# Layering: chat_handlers' transitive import graph now reaches back into this
# module (chat_handlers -> remote_adopt -> handlers_instances -> session_transfer),
# so a MODULE-LEVEL import of chat_handlers here closes an import cycle: whichever
# of the two loads first hits the other while it is still partially initialised.
# The two symbols this module needs (``_materialise_slot_from_history`` and
# ``_redact_history_rows``) are used only inside ``api_chat_slot_import``, so they
# are imported FUNCTION-LOCALLY at the top of that handler instead. Keep it that
# way: a module-level import reinstates the cycle. The proper long-term fix is to
# move the shared collaborators down to chat_persistence, per the note there.
from kiro_crew.dashboard.arrival_folders import (
    arrival_folder_exists,
    arrival_folder_id,
    discard_arrival_folders,
    mark_arrival_folder_shared,
)
from kiro_crew.dashboard.chat_persistence import (
    save_slot_off_loop,
    session_transcript_remains,
    session_was_deleted,
)
from kiro_crew.dashboard.chat_utils import (
    _sync_dashboard_slots,
    effective_session_key,
    slot_history_key,
)
from kiro_crew.dashboard.state import MAX_LIVE_SLOTS, DashboardState, _ChatSlot
from kiro_crew.dashboard.token_auth import effective_request_app
from kiro_crew.security import (
    redact_credentials,
    redact_exfiltration_urls,
    redact_local_paths,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Bundle schema version. Bump on any incompatible change to the payload shape;
#: the importer refuses a version it does not know rather than guessing, because
#: the two ends of a transfer are independently-updated installs and a silently
#: misread field would land as corrupted conversation.
BUNDLE_VERSION = 2

#: Versions this importer still accepts. v1 = transcript-only (the peer rebuilds
#: context via the lossy ~8K history prefix); v2 additionally carries **Layer B**
#: — the kiro-cli context window (``<sid>.json`` + ``<sid>.jsonl``) — so an
#: imported session resumes with full fidelity via ``session/load`` instead of a
#: replayed prefix. Accepting both lets a newer instance still receive a copy
#: from an older one; anything OUTSIDE the set is refused rather than
#: best-effort parsed, because a silently misread field lands as corrupted
#: conversation.
_SUPPORTED_BUNDLE_VERSIONS = (1, 2)

#: Per-bundle limits. A bundle arrives from another instance, so it is untrusted
#: input even though the peer is one the owner configured: these bound the work
#: a single request can cause before any of it is written to disk.
_MAX_MESSAGES = 5_000
_MAX_CONTENT_CHARS = 1_000_000
_MAX_TITLE_CHARS = 500
_MAX_TOTAL_CHARS = 20_000_000

#: Cap on the Layer B events blob (``<sid>.jsonl``). Larger than the transcript
#: cap because Layer B also carries tool/system frames and the full context the
#: model actually holds, but still bounded: an oversized blob is refused before
#: anything is written, so a peer cannot make an import exhaust disk or memory.
_MAX_LAYER_B_CHARS = 40_000_000

#: Structural allowance over the two content ceilings: the keys, quotes, commas
#: and ``\uXXXX`` escapes a bundle sitting at both ceilings still needs. Named
#: rather than folded into the total so the derivation below stays readable.
_JSON_ENVELOPE_SLACK = 8 * 1024 * 1024

#: Ceiling on the DECOMPRESSED request body. A compressed upload is an amplifier
#: — a megabyte of gzip expands to roughly a gigabyte of repeated bytes — so the
#: expansion has to be bounded before it is materialised, not after.
#:
#: **What makes this safe is the comparison to the gateway's own body limit, not
#: the arithmetic below.** The Application's ``client_max_size`` is 60 MiB and
#: applies to every body, compressed or not, so the PLAIN path can never deliver
#: more than 60 MiB of JSON. This ceiling is above that, which means the gzip path
#: accepts strictly MORE than the plain path can: a bundle refused here is a
#: bundle the plain path refuses too.
#:
#: The magnitude is taken from the validator's own ceilings —
#: ``_MAX_TOTAL_CHARS`` of transcript plus ``_MAX_LAYER_B_CHARS`` of Layer B
#: events, plus envelope slack — so the number moves with them rather than being
#: chosen freshly. It is deliberately NOT the worst-case ENCODED width: those
#: ceilings count CHARACTERS, and ``json.dumps(ensure_ascii=True)`` renders one
#: non-ASCII character as a six-byte ``\uXXXX`` escape, so a bundle that is valid
#: by character count can be several times larger in bytes. Sizing for that worst
#: case would mean admitting a ~360 MB allocation on an authenticated write route
#: to accommodate a session of ~11M CJK characters — which ``client_max_size``
#: refuses on the plain path anyway. The bound stays where it protects memory, and
#: the bundle that theoretically loses out is one no route has ever accepted.
_MAX_DECOMPRESSED_BYTES = _MAX_TOTAL_CHARS + _MAX_LAYER_B_CHARS + _JSON_ENVELOPE_SLACK

#: The gateway Application's own body limit (``dashboard/server.py``), restated so
#: the invariant above can be tested rather than asserted in prose.
_GATEWAY_CLIENT_MAX_SIZE = 60 * 1024 * 1024

#: How many bodies may be expanding at once, and how many may be waiting to.
#:
#: :data:`_MAX_DECOMPRESSED_BYTES` bounds ONE request; without a concurrency bound
#: N authenticated requests each hold up to that much — first as bytes, then as
#: the parsed document — for as long as their arrival takes, and the sum is what
#: exhausts the host rather than any single body. So a permit covers the whole
#: arrival, not just the expansion: see :func:`_read_bundle_body`. Two in flight
#: bounds resident expansion to roughly twice the ceiling; a small queue absorbs
#: ordinary bursts (a person installing several files) while anything past it is
#: refused immediately rather than parked, because a queue that grows without
#: limit is the same failure with a delay in front of it.
_MAX_CONCURRENT_EXPANSIONS = 2
_MAX_QUEUED_EXPANSIONS = 4

#: Guards the two counters below. A plain lock rather than a semaphore because the
#: WAITING count has to be testable before waiting, which a semaphore does not
#: expose. Loop-bound: created lazily so importing this module binds no loop.
_expansion_lock: asyncio.Lock | None = None
_expansion_slots: asyncio.Semaphore | None = None
_expansion_waiting = 0

#: Output granularity of the bounded gunzip. Small enough that refusing a bomb
#: costs one chunk of memory, large enough that a real 60 MiB bundle is a few
#: hundred iterations rather than a few hundred thousand.
_CHUNK_BYTES = 256 * 1024

#: gzip's own framing magic (RFC 1952 §2.3.1). The body format is sniffed from
#: these two bytes and NOT from ``Content-Type``: the export endpoint answers
#: ``application/gzip``, a browser upload of that same file may send
#: ``application/octet-stream`` or nothing at all, and the tunnel's
#: server-to-server caller sends ``application/json``. Sniffing the bytes keeps
#: all three working without asking any caller to relabel what it already sends.
_GZIP_MAGIC = b"\x1f\x8b"

#: How many times to re-take the transcript snapshot when the periodic flush
#: lands inside the off-loop read. Small on purpose: the flush is 5s-periodic, so
#: even one interleave is rare and a second is vanishingly unlikely. Exhausting
#: these falls back to a guaranteed-consistent inline read rather than shipping a
#: transcript that might be missing turns.
_SNAPSHOT_ATTEMPTS = 4


class SnapshotUnstable(RuntimeError):
    """No consistent view of the source transcript could be taken.

    Two causes: the periodic flush kept landing inside the off-loop read, or a
    rewind/regenerate rewrite is still owed so the on-disk transcript is stale.

    Raised instead of bundling anyway or falling back to a blocking inline read.
    A transfer is a copy, so failing it is cheap and the caller can retry, whereas
    shipping the bundle would send the wrong conversation and a synchronous read
    of a large transcript on the event loop can starve the liveness heartbeat
    until the watchdog exits the gateway.
    """


#: Roles that make up a visible conversation. Tool/system frames are not carried:
#: they reference local tool state that means nothing on the target instance.
_VISIBLE_ROLES = ("user", "assistant")

#: Prefix marking an imported session in the sidebar, so a transferred tab is
#: never mistaken for one that originated locally.
_IMPORT_TITLE_MARKER = "⇄ "


def local_instance_label() -> str:
    """A short human label for THIS instance, used as a transfer's ``origin``.

    The local instance is implicit in the registry and has no configured name
    (instances.md §1), so there is nothing to read: the host's first DNS label
    is the most recognisable stand-in and is short enough to sit in a session
    title. Falls back to ``"another instance"`` rather than raising, because a
    missing label must never fail a transfer.
    """
    try:
        return platform.node().split(".")[0] or "another instance"
    except Exception:
        return "another instance"


def _read_chained_history(state: DashboardState, session_key: str) -> list[dict]:
    """Read a session's full on-disk transcript. **Blocking** — file IO + JSON.

    Split out so a caller on the event loop can push it to a thread; see
    :func:`build_transfer_bundle_async`.
    """
    if state.conversation_log:
        return state.conversation_log.read_messages_chained(session_key)
    return []


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
    for line in events.split("\n"):
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
    if not sid:
        return None
    try:
        d = kiro_sessions_dir()
        jf = d / f"{sid}.json"
        lf = d / f"{sid}.jsonl"
        if not jf.exists() or not lf.exists():
            return None
        # Cap BEFORE the read, not after. ``read_text`` on a multi-gigabyte
        # tool-output log allocates the whole blob first, so a post-read ``len``
        # check bounds nothing -- the allocation that OOMs the gateway has
        # already happened by the time it runs. ``st_size`` is the only bound
        # available ahead of the allocation, and it covers the ENVELOPE too: that
        # read is unbounded on the same path, and a session's ``.json`` grows
        # with its own metadata.
        #
        # This makes the ceiling effectively a BYTE cap where the name says
        # chars. For multibyte text that is strictly tighter -- a 40M-char CJK
        # log is ~120MB, so it degrades to transcript-only where the char cap
        # alone would load it -- and that is the correct direction for a limit:
        # the ceiling has to bound what is actually allocated, and the fallback
        # is an honest transcript-only copy rather than a crashed gateway. The
        # char check below stays as the semantic cap.
        for f in (jf, lf):
            if f.stat().st_size > _MAX_LAYER_B_CHARS:
                logger.debug(
                    "session_transfer: Layer B file %s exceeds the %d-byte cap; "
                    "sending transcript-only",
                    f.name,
                    _MAX_LAYER_B_CHARS,
                )
                return None
        envelope = json.loads(jf.read_text(encoding="utf-8"))
        events = lf.read_text(encoding="utf-8")
    except Exception:
        logger.debug("session_transfer: could not read Layer B for sid=%s", sid, exc_info=True)
        return None
    if not isinstance(envelope, dict) or len(events) > _MAX_LAYER_B_CHARS:
        return None
    if not _events_jsonl_is_loadable(events):
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
    return {"sid": sid, "envelope": envelope, "events": events}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_source_record(
    *,
    model: str = "",
    reasoning_effort: str = "",
    approval_policy: str | None = None,
    workspace: str = "",
    project: str = "",
) -> dict[str, Any]:
    """Assemble the bundle's ``source`` provenance record. Pure — thread-safe.

    **Recorded, never applied.** Every field here describes what the source
    session ran under, for a reader to look at; no import path reads any of it.
    ``approval_policy`` is the one that has to be said out loud: ``"auto"`` means
    auto-approve every tool, so an *applied* copy would let a session arrive on
    another machine pre-authorised to run tools without prompting — the same
    class of escalation ``subagent._validate_agent`` refuses when it declines to
    default an unknown agent name. An installed session always lands interactive.

    **The reader is a person, not a caller.** An export is a user-facing artifact
    whose whole point is being inspectable, and somebody deciding whether to
    install a session needs to know what model produced it, at what effort, and
    above all whether the transcript was produced under auto-approval. Every
    field here earns its place against that reader; a field that only a future
    caller would want does NOT go in, which is why ``mode`` and
    ``autocompact_pct`` are absent — both are re-derived per turn, so they are
    pointless to apply and there is nothing for a human to do with them either.

    ``origin`` and ``agent`` are NOT repeated here: both already sit at the top
    level of the bundle, where the importer reads them.

    **A field is omitted rather than written empty.** Absence means "not known",
    which is a distinct and useful statement, and it is also the format's own
    compatibility mechanism: a reader asks whether a key is present and
    well-formed, never whether a version implies it must be there, so every key
    must be safe to leave out.

    ``approval_policy`` is the exception to that rule, because for it an empty
    string is a VALUE and not an absence — ``""`` is the interactive policy, the
    same spelling the session object uses. It is therefore keyed off ``None``
    (this gateway could not read a policy) rather than off emptiness, so
    "interactive" and "unknown" stay distinguishable. Collapsing them would make
    the field's only interesting reading — that a transcript was produced under
    auto-approval — indistinguishable from a gateway that had nothing to report.
    """
    source: dict[str, Any] = {}
    if model:
        source["model"] = model
    if reasoning_effort:
        source["reasoning_effort"] = reasoning_effort
    if approval_policy is not None:
        source["approval_policy"] = approval_policy
    # ``workspace`` and ``project`` are the only FREE TEXT in this record -- a
    # workspace name and a checkout path, both of which a user chose. The bundle
    # is an egress boundary and the same scan already runs over the title and over
    # assistant content, so it runs here too rather than leaving two unscanned
    # strings in a document that leaves the host. The credential/URL passes alone
    # miss a bare host path (``/local/home/<login>/...``): that shape carries no
    # credential yet still discloses the operator's login and on-disk layout to
    # whoever the file is shared with, so ``redact_local_paths`` runs as well.
    # The imported session drops ``project`` anyway (module docstring), so a
    # ``[redacted-path]`` placeholder costs the human reader nothing.
    if workspace:
        scrubbed, _ = redact_exfiltration_urls(workspace)
        scrubbed, _ = redact_credentials(scrubbed)
        scrubbed, _ = redact_local_paths(scrubbed)
        source["workspace"] = scrubbed
    if project:
        scrubbed, _ = redact_exfiltration_urls(project)
        scrubbed, _ = redact_credentials(scrubbed)
        scrubbed, _ = redact_local_paths(scrubbed)
        source["project"] = scrubbed
    source["exported_at"] = _iso_now()
    # Which code wrote the file, for diagnosis when a key is unexpectedly absent.
    # NEVER read as a gate: a version number cannot answer that question across
    # forks, because two forks can stamp the same number on different formats.
    # Feature detection is what protects a reader; this only explains, after the
    # fact, why a feature was missing.
    source["producer"] = f"kirocrew/{__version__}"
    return source


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


def _snapshot_source_record(
    state: DashboardState, slot: _ChatSlot, session_key: str
) -> dict[str, Any]:
    """Snapshot *slot*'s provenance for the bundle. **MUST run on the event loop.**

    Every value read here lives on the slot or on the live session registry, so
    the read has to happen where the loop owns them — and in the same breath as
    the transcript tail, so what the file says the session ran under matches the
    turns the file carries.

    The tool-approval policy is read from the LIVE session
    (``SessionManager.get_approval_policy``), which is its only home: it is
    per-session runtime state with no durable copy anywhere. So a conversation
    whose session object is gone — evicted, or not yet re-opened after a gateway
    restart — has no policy to report, and ``has_session`` is what separates that
    from a session that is live and interactive. The unknown case records nothing
    rather than guessing ``""``, because guessing would report a transcript
    produced under auto-approval as an interactive one.

    *session_key* is the caller's PINNED key and is deliberately not recomputed
    here. The slot's binding can move while the bundle is being assembled — a cron
    injection rebinds ``linked_session_key`` — and the transcript key was pinned
    before the pre-bundle flush. Reading the policy off a freshly resolved key
    would then describe a session the shipped transcript never ran under, and a
    downloaded file has no way to correct itself later. The values that DO come
    from the slot (model, effort, workspace, project) are still read per attempt,
    because those must follow the tail this attempt is shipping.
    """
    approval_policy: str | None = None
    sessions = getattr(state, "sessions", None)
    if sessions is not None:
        try:
            if sessions.has_session(session_key):
                approval_policy = sessions.get_approval_policy(session_key) or ""
        except Exception:
            # Provenance is never worth failing an export for; an unreadable
            # registry simply means the policy is unknown, which the record can
            # say by leaving the field out.
            logger.debug(
                "session_transfer: could not read the approval policy for slot=%s",
                slot.key,
                exc_info=True,
            )
    return build_source_record(
        model=slot.model or "",
        reasoning_effort=slot.reasoning_effort or "",
        approval_policy=approval_policy,
        workspace=slot.workspace or "",
        project=slot.project or "",
    )


async def build_transfer_bundle_async(
    state: DashboardState,
    slot: _ChatSlot,
    *,
    origin: str = "",
    with_source: bool = False,
    include_layer_b: bool = True,
) -> dict[str, Any]:
    """Serialise *slot*'s visible conversation into a portable bundle, with the
    disk read off the event loop.

    Carries the FULL conversation rather than only the window currently held in
    memory — a long-running session keeps just its tail resident, and bundling
    ``slot.messages`` alone would silently truncate the transfer to that tail.
    *origin* is a human label for where the session came from (an instance name
    or ``"local"``); it is recorded for provenance and shown on arrival.

    *with_source* adds the ``source`` provenance record of
    :func:`build_source_record`. It is OFF by default so the tunnel keeps
    producing exactly the bundle it produces today: a peer's importer would drop
    the key harmlessly, but a send is an existing working flow and this feature
    has no business changing what it puts on the wire. The file export turns it
    on, because a file outlives the tab it came from and a reader of one has
    nothing else to tell them what the session ran under.

    *include_layer_b* is the gate on the model's context window; it defaults to
    carrying Layer B. The tunnel send uses that default, so a copy pushed between
    two live gateways RESUMES rather than replaying a lossy prefix. The file
    export does NOT use the default: it passes ``True`` only when the operator has
    opted in both at the config layer (``dashboard.export_include_layer_b``, off by
    default) and on the specific request, because a downloaded file can be shared
    with another person and unredacted context must not ride along unasked (the
    RFC's conjunctive minimum bar, rfc-s3-backup.md:317-319; the risk is the
    operator's per O1). Layer B ships byte-exact and unredacted (see
    :func:`_read_layer_b`), which is forced rather than chosen -- the thinking-block
    signatures inside it are validated on replay, so redacting and transplanting
    cannot both hold, and there is no redacted variant. A caller passing ``False``
    withholds it and the bundle sets ``layer_b_skipped``, so the lost resume
    fidelity is stated rather than inferred from an absent key. Even when a caller
    asks to carry Layer B, this builder still withholds it for a mid-turn snapshot
    (see below), using the same ``layer_b_skipped`` flag; that consistency decision
    is independent of the caller's gate. A session that never opened a kiro-cli
    context sets neither ``layer_b`` nor ``layer_b_skipped``, because there is no
    context to lose.

    The un-flushed tail is a ``_disk_window_len`` boundary slice, which is valid
    only because the flush below runs first: the save folds a durable injector's
    ``append_if_absent`` copy into the window and advances the boundary, so the
    counter is honest by the time the tail is snapshotted. A caller that bundled
    WITHOUT flushing could not use this slice — a durable injector
    (``cron_inject``, ``workflow_inject``) puts the same row into
    the window and onto disk without a save, so the boundary would start one row
    too early and ship the injection twice. There is deliberately no such
    caller: this is the only builder, and it always flushes.

    The transcript read is synchronous file IO plus JSON parsing over a whole
    session, which is exactly the "large synchronous file IO" the
    ``no-blocking-call-on-event-loop`` rule forbids on the loop: on a long
    session it stalls every other task, and because the liveness heartbeat is
    itself a coroutine a stalled loop cannot pet LoopStallWatchdog, which then
    exits the gateway.

    **Offloading introduces an await, so the snapshot must be checked for
    consistency.** While we are off the loop the periodic 5s flush can run: it
    writes the dirty tail to disk AND advances ``_resumed_count`` / clears
    ``_dirty``. If that lands between our read and our merge, a naive merge reads
    pre-flush disk content and then sees a clean slot — silently dropping the
    tail from the copy.

    Because a completed flush advances ``_disk_window_len`` (the persisted
    boundary) as it writes, an unchanged value across the await is positive proof
    that no flush landed: ``history`` then corresponds exactly to
    ``messages[:_disk_window_len]``, so the tail merge is consistent. On a change
    we retry against the new state. Messages arriving during the await are
    harmless — they extend the tail we are about to copy, they do not move the
    boundary.

    If the retries are exhausted (a flush would have to land inside every one of
    them, which the 5s cadence makes effectively impossible), the transfer
    **fails** with :class:`SnapshotUnstable`. It deliberately does not fall back
    to an inline read: that would trade a lossy transcript for a blocking one,
    and on a large active session the blocking read is what starves the heartbeat
    into a watchdog-triggered gateway exit. Failing costs nothing here — a
    transfer is a copy, so the source is untouched and the user can just retry —
    which makes it strictly better than either losing turns or wedging the
    gateway.
    """
    # slot_history_key, NOT effective_session_key: this addresses a TRANSCRIPT
    # PATH, and for a channel-born slot the dashboard could not bind, the session
    # key resolves to ``dashboard:<stem>`` — a file no read path uses. Bundling
    # from that phantom transcript would ship only the resident window and
    # silently drop every older turn. chat_utils documents the split.
    key = slot_history_key(slot)
    # The session_map is keyed by the SESSION key (what turns run on), which for
    # a channel-bound slot differs from the transcript key above. Resolve it on
    # the loop (pure getattr) and hand it to the thread, so Layer B is read from
    # exactly where the resume path will later look for it.
    sm_key = effective_session_key(slot)
    # Resolve the Layer B sid HERE, on the loop: the lookup self-prunes the
    # session map, so it cannot go into the worker thread below (see
    # _resolve_layer_b_sid). The thread receives only an immutable string.
    #
    # SKIP Layer B entirely while a turn is in flight. Layer A records the user's
    # prompt as soon as it is submitted, but kiro-cli only writes Layer B when the
    # turn persists -- so a mid-turn bundle pairs a transcript that SHOWS the
    # prompt with a context that does not contain it, and the peer's
    # ``session/load`` would resume the model behind its own visible transcript.
    # That skew is specific to carrying Layer B; Layer A alone has no such
    # coupling. Degrading to transcript-only is the honest outcome and is already
    # plumbed end to end -- the import reports ``resume_mode: prefix`` and the
    # sender's row reads "Sent (transcript only)" -- so the user is told, rather
    # than being handed a silently divergent copy or a hard failure on a
    # legitimate action. ``_in_stage_execution`` is included because ``running``
    # reads False between the stages of a staged plan (chat_handlers).
    #
    # Computed INSIDE the retry loop below, never once up front: a retry happens
    # precisely because the slot changed, and a prompt starting during a threaded
    # read is one such change -- so a pre-loop value would let the retry pick up
    # the new prompt in Layer A while still shipping the pre-turn Layer B, which
    # is exactly the skew this check exists to prevent.
    _guard_snapshot(slot)
    # Persist a dirty slot BEFORE snapshotting. The tail slice only sees messages
    # at or past the boundary, so an edit made IN PLACE below it — a variant
    # switch replacing an already-persisted assistant turn — is invisible to it.
    # If that edit's own save failed, disk still holds the previous response and
    # the copy would ship it.
    #
    # Flushing here is safe because the save advances ``_disk_window_len`` itself,
    # so afterwards the tail slice is empty and the bundle comes wholly from disk.
    # Slicing on ``_resumed_count`` instead would duplicate the tail: the save
    # does NOT touch that counter.
    #
    # best_effort=False: a swallowed failure would put us right back to bundling
    # a stale transcript, so an unpersistable source fails the transfer instead.
    # The source is otherwise untouched — a flush persists what is already in
    # memory, it does not change the conversation.
    for _attempt in range(_SNAPSHOT_ATTEMPTS):
        # Flush on EVERY attempt, not once before the loop. A retry happens
        # precisely BECAUSE the slot changed, and that change is unpersisted, so
        # re-reading disk without flushing first would serialize the superseded
        # content — the exact staleness this flush exists to prevent.
        if slot._dirty:
            # The flush is itself an await, so an edit can land inside it: the
            # save writes the snapshot it captured on entry, leaving disk on the
            # EARLIER content while the slot is already newer. Pin the generation
            # across this await and spend an attempt rather than trusting it.
            gen_before_save = slot._dirty_gen
            try:
                saved = await save_slot_off_loop(state, slot, best_effort=False)
            except Exception as exc:
                logger.warning(
                    "session_transfer: could not persist slot=%s before bundling",
                    slot.key,
                    exc_info=True,
                )
                raise SnapshotUnstable("the session could not be persisted before copying") from exc
            if not saved:
                # Delete-won: the session was permanently deleted while the
                # flush awaited the lock. Bundling would ship the destroyed
                # conversation to the peer (or an empty shell of it), so the
                # transfer fails instead of answering success.
                logger.warning(
                    "session_transfer: slot=%s was permanently deleted during "
                    "the pre-bundle flush; refusing the transfer",
                    slot.key,
                )
                raise SnapshotUnstable("the session was permanently deleted")
            if slot._dirty_gen != gen_before_save:
                continue
            _guard_snapshot(slot)
        boundary_before = slot._disk_window_len
        # ``_dirty_gen`` is the primary marker: a monotonic counter the ``_dirty``
        # setter bumps centrally, so ANY mutation that marks the slot dirty moves
        # it — including an edit made IN PLACE, like a variant switch replacing an
        # already-persisted turn. Neither the boundary nor the message count moves
        # for that, so without this the copy could carry a superseded response.
        gen_before = slot._dirty_gen
        # The boundary catches the one mutation gen does NOT: a completed flush
        # advances ``_disk_window_len`` without marking the slot dirty.
        #
        # The count is a backstop for any path that mutates ``slot.messages``
        # without marking dirty. Strictly redundant against a correct dirty-mark,
        # kept because this snapshot has already been wrong twice by assuming a
        # single field told the whole story.
        count_before = len(slot.messages)
        # Direct delete check, independent of the flush arm above: if the
        # periodic 5s flush hit the delete-won guard first, it cleared
        # ``_dirty``, the flush arm here never ran, and the disk read below
        # would assemble a bundle from a permanently deleted session (its
        # in-memory tail plus an empty transcript). The ``saved``-check above
        # only covers a delete observed by THIS builder's own flush.
        if session_was_deleted(state, slot):
            logger.warning(
                "session_transfer: slot=%s belongs to a permanently deleted "
                "session; refusing the transfer",
                slot.key,
            )
            raise SnapshotUnstable("the session was permanently deleted")
        # Snapshot the unpersisted tail (and the slot fields the bundle needs) ON
        # THE LOOP, so the thread below never touches the slot while the loop
        # could be appending to it. Everything past this point is plain data.
        tail = list(slot.messages[boundary_before:])
        title = slot.title if slot._titled else ""
        agent = slot.agent
        # Snapshotted per attempt alongside the tail, for the same reason: a
        # retry happens because the slot CHANGED, so a record taken before the
        # loop could describe a model or a policy the shipped transcript never
        # ran under.
        #
        # The SESSION key is the exception and is passed in pinned. The transcript
        # key was fixed before the flush, so the session the shipped turns ran on
        # is already decided; re-resolving it here would let a rebind landing in
        # the flush await pair this transcript with another session's approval
        # policy.
        source = _snapshot_source_record(state, slot, sm_key) if with_source else None
        # Layer B eligibility is decided HERE, per attempt, on the loop and in the
        # same breath as the tail snapshot -- so the transcript and the context we
        # ship always come from one consistent view of the slot. See the note
        # above for why a pre-loop value goes stale across a retry.
        mid_turn = bool(getattr(slot, "running", False)) or bool(
            getattr(slot, "_in_stage_execution", False)
        )
        if not include_layer_b:
            # Withheld because this caller's policy gate resolved false -- the
            # decision belongs to the call site, not this builder. The file
            # export withholds Layer B by default and carries it only for a
            # dashboard operator's twofold opt-in: standing config permission plus
            # an explicit per-invocation flag. The tunnel send requests Layer B
            # by default, but this builder still withholds it for a mid-turn snapshot.
            # Do not restate more destination policy here: the caller decided, and
            # the decision (and its rationale) lives at the call site.
            #
            # The sid is still resolved first, and ONLY to answer whether there was
            # anything to withhold. ``layer_b_skipped`` means "this session HAD
            # context and it was given up", and the importer appends a
            # "transcript only" suffix to the tab title on the strength of it. A
            # session that never opened a kiro-cli context gave up nothing, so
            # flagging it would label an undegraded copy as degraded -- the
            # cry-wolf case ``_assemble_bundle`` warns about, on every such
            # withheld export.
            layer_b_withheld = bool(_resolve_layer_b_sid(getattr(state, "sessions", None), sm_key))
            layer_b_sid = ""
        elif mid_turn:
            layer_b_sid = ""
            layer_b_withheld = True
            logger.info(
                "session_transfer: slot=%s has a turn in flight; sending transcript-only "
                "(Layer B would lag the displayed transcript)",
                slot.key,
            )
        else:
            layer_b_sid = _resolve_layer_b_sid(getattr(state, "sessions", None), sm_key)
            layer_b_withheld = False
        # Read AND assemble off the loop. Assembly redacts every assistant turn,
        # and the transcript can run to the bundle cap, so those regex scans are
        # far too much CPU to hold the loop with — the same starvation that
        # exits the gateway via LoopStallWatchdog.
        bundle = await asyncio.to_thread(
            _read_and_assemble,
            state,
            key,
            tail,
            title,
            agent,
            origin,
            layer_b_sid,
            layer_b_withheld,
            source,
        )
        # Re-check the guards AFTER the await, not only before it. A rewind or a
        # mid-stream flush can land during the threaded read, and the boundary
        # alone does not reveal a rewind: ``_pending_rewrite`` can flip to True
        # while ``_disk_window_len`` stays put, which would otherwise read as
        # "stable" and copy turns the user just discarded.
        _guard_snapshot(slot)
        # The deletion check too: the assembly read above is the longest await
        # in this builder (redaction regexes over the whole transcript), so a
        # permanent delete can complete inside it — after the pre-read probe
        # passed — and the bundle in hand is the destroyed conversation. A
        # delete is permanent, so this is a refusal, not a retry.
        if session_was_deleted(state, slot):
            logger.warning(
                "session_transfer: slot=%s was permanently deleted during "
                "bundle assembly; refusing the transfer",
                slot.key,
            )
            raise SnapshotUnstable("the session was permanently deleted")
        if (
            slot._dirty_gen == gen_before
            and slot._disk_window_len == boundary_before
            and len(slot.messages) == count_before
        ):
            return bundle
        logger.debug(
            "session_transfer: slot %s flushed during the transcript read; retrying",
            slot.key,
        )
    raise SnapshotUnstable(f"transcript snapshot did not settle in {_SNAPSHOT_ATTEMPTS} attempts")


def _guard_snapshot(slot: _ChatSlot) -> None:
    """Refuse to bundle from a slot whose disk view cannot be trusted.

    Called both before and after every awaited read — see the call sites.
    """
    # A rewind/regenerate marks the slot ``_pending_rewrite`` and only clears it
    # once the TRUNCATING rewrite has been written. While it is set, disk still
    # holds the PRE-EDIT transcript and is longer than the resident window, so the
    # boundary slice appends nothing and the bundle would carry turns the user
    # explicitly rewound away.
    if slot._pending_rewrite:
        raise SnapshotUnstable("a pending rewrite means the on-disk transcript is stale")
    # The boundary can also run AHEAD of the resident window, and then the tail
    # slice silently yields nothing. ``_save_slot_to_history`` sets
    # ``_disk_window_len = len(window)`` over the RAW window, streaming ``chunk``
    # rows included; ``_flush_segment`` then reassigns ``slot.messages`` to drop
    # that trailing chunk run and append the finalized assistant message, without
    # adjusting the boundary. (Memory trimming keeps the two in step; this does
    # not.)
    if slot._disk_window_len > len(slot.messages):
        raise SnapshotUnstable(
            "the persisted boundary is ahead of the resident window " "(a flush landed mid-stream)"
        )


def _read_and_assemble(
    state: DashboardState,
    session_key: str,
    tail: list[dict],
    title: str,
    agent: str,
    origin: str,
    layer_b_sid: str = "",
    layer_b_skipped: bool = False,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read the transcript + Layer B and assemble the bundle. **Runs in a thread.**

    Touches no slot state and no session map — *tail*, *title*, *agent*,
    *layer_b_sid* and *source* are all snapshots the caller took on the event
    loop — so it is safe off-loop. Only the file reads happen here.
    """
    history = _read_chained_history(state, session_key)
    history.extend(tail)
    layer_b = _read_layer_b(layer_b_sid)
    if layer_b_sid and layer_b is None:
        # A sid was MAPPED but its files would not read -- pruned, over the size
        # cap, or unparseable JSONL. That is context this session genuinely had
        # and is now giving up, which is the sender's other degradation case: the
        # peer must be told, or the receiving tab shows a full-looking copy with
        # no resumable context behind it. Distinct from ``layer_b_sid == ""``,
        # which means there was never a context to carry.
        layer_b_skipped = True
    return _assemble_bundle(history, title, agent, origin, layer_b, layer_b_skipped, source)


def _assemble_bundle(
    all_messages: list[dict],
    title: str,
    agent: str,
    origin: str,
    layer_b: dict[str, Any] | None = None,
    layer_b_skipped: bool = False,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn a merged transcript into the wire bundle. Pure — thread-safe.

    Kept free of slot access on purpose: the redaction below is regex-heavy over
    up to the whole transcript, so :func:`build_transfer_bundle_async` runs this
    in a thread, and anything touching ``slot`` there would race the event loop.

    *source* is the optional provenance record of :func:`build_source_record`.
    It is omitted when empty and the tunnel path passes none, so a bundle sent
    over a tunnel is byte-identical with and without this feature.
    """
    messages: list[dict[str, Any]] = []
    for m in all_messages:
        role = m.get("role")
        if role not in _VISIBLE_ROLES:
            continue
        content = m.get("content", "")
        # Redact on the way OUT, not only on the way in. This bundle leaves the
        # host, so this is an egress boundary: a transcript already on disk (or one
        # carried in from a channel) can still hold a raw credential, and relying
        # on the peer to scrub it would send
        # the secret across the boundary first and trust the far side to clean up.
        # The importer redacts again — idempotent, and it must not assume a
        # well-behaved sender.
        #
        # User turns stay verbatim, matching the fork and import paths: redacting
        # what the human typed would corrupt their own words.
        if role != "user":
            content, _ = redact_exfiltration_urls(content)
            content, _ = redact_credentials(content)
        messages.append({"role": role, "content": content, "ts": m.get("ts", "")})

    # Strip our own marker so a session bounced back and forth does not
    # accumulate one prefix per hop.
    title = title.removeprefix(_IMPORT_TITLE_MARKER)
    # Titles are egress too. A title is generated from user content, and the
    # resume path assigns a client-supplied ``body["title"]`` with no scan of its
    # own, so a resumed title can carry a credential that would otherwise leave
    # the host verbatim. The importer redacts again; this is the boundary.
    # A title generated after a file operation also names a checkout path, so it
    # gets the path scrub too: a title is a short label, not substance, so
    # replacing a path with a placeholder there costs the reader nothing.
    title, _ = redact_exfiltration_urls(title)
    title, _ = redact_credentials(title)
    title, _ = redact_local_paths(title)
    bundle: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "origin": origin,
        "title": title,
        # Hint only — the importer drops it unless the target has this agent.
        "agent": agent,
        "messages": messages,
    }
    # Layer B rides along only when the session has one. Its events were already
    # egress-redacted in :func:`_read_layer_b`; the envelope carries no secret
    # (its paths and title are neutralised on import).
    if layer_b:
        bundle["layer_b"] = {"envelope": layer_b["envelope"], "events": layer_b["events"]}
    elif layer_b_skipped:
        # An EXPLICIT degradation flag, because an absent ``layer_b`` is ambiguous
        # on its own: it means either "this session never had a kiro-cli context"
        # (nothing was lost -- flagging it would cry wolf on every such import) or
        # "the source was mid-turn, so shipping context would have lagged the
        # transcript" (something WAS given up, and the receiving tab should say
        # so). Only the sender can tell those apart, so it says which.
        bundle["layer_b_skipped"] = True
    # Provenance, and only on a path that asked for it. An empty record is left
    # out entirely rather than written as ``{}``: the whole point of the key is
    # that a reader probes for it, so an empty object would be a claim to carry
    # provenance that carries none.
    if source:
        bundle["source"] = dict(source)
    return bundle


def bundle_rejection_reason(bundle: dict[str, Any]) -> tuple[str, str]:
    """Why THIS instance's own importer would refuse *bundle*, or ``("", "")``.

    Exists so a producer can refuse to hand over a document its own reader would
    reject. The bounds live in one place -- :func:`_validate_bundle` -- and this
    runs that same function rather than restating its limits, because a second
    copy of "5 000 messages, 20 000 000 chars" is a copy that drifts.

    Returns ``(reason, code)`` from the validator's own coded rejection. The
    validated payload is deliberately DISCARDED: validation rebuilds a normalised
    allowlist, so shipping its output would silently drop the optional keys a
    caller added on purpose. Only the verdict is taken.
    """
    _, err = _validate_bundle(bundle)
    if err is None:
        return "", ""
    # ``Response.body`` is typed as bytes-or-Payload; the validator always builds a
    # JSON response, so narrow rather than assume.
    raw = err.body if isinstance(err.body, (bytes, bytearray)) else b""
    try:
        body = json.loads(raw or b"{}")
    except Exception:  # pragma: no cover - the validator always writes JSON
        return "the bundle was refused", "transfer_bundle_invalid"
    return str(body.get("error", "the bundle was refused")), str(body.get("code", ""))


def _reject(reason: str, code: str) -> web.Response:
    """Return a 400 validation failure carrying a machine-readable ``code``.

    Every non-2xx body here needs ``code``: ``test_error_code_contract.py``
    ratchets on it, and a coded body is what lets the sending instance
    distinguish "peer is too old to understand this bundle" from "bundle was
    malformed" without parsing prose.

    The status is a literal 400 rather than a parameter on purpose — the
    contract gate reads the status statically, and a variable one lands in its
    "cannot decide" bucket. The single non-400 rejection (the slot cap) spells
    its own status out at the call site.
    """
    return web.json_response({"error": reason, "code": code}, status=400)


class _BundleTooLarge(Exception):
    """The decompressed body ran past :data:`_MAX_DECOMPRESSED_BYTES`.

    Its own type, not a size returned alongside the bytes, because the whole
    point is that the bytes are never produced: the caller has to be able to
    tell "refused while expanding" apart from "expanded, then measured".
    """


class _ExpansionBusy(Exception):
    """Too many bodies are already expanding or waiting to expand."""


@contextlib.asynccontextmanager
async def _expansion_admission() -> Any:
    """Admit one decompression, or refuse. **Loop-bound.**

    Bounds resident expansion to :data:`_MAX_CONCURRENT_EXPANSIONS` times the
    per-body ceiling. A caller past the queue limit is refused straight away
    rather than parked, so the waiting set cannot itself become the allocation.

    Raises:
        _ExpansionBusy: when the queue is full.
    """
    global _expansion_lock, _expansion_slots, _expansion_waiting
    if _expansion_lock is None:
        _expansion_lock = asyncio.Lock()
    if _expansion_slots is None:
        _expansion_slots = asyncio.Semaphore(_MAX_CONCURRENT_EXPANSIONS)

    async with _expansion_lock:
        if _expansion_waiting >= _MAX_QUEUED_EXPANSIONS:
            raise _ExpansionBusy(_expansion_waiting)
        _expansion_waiting += 1
    try:
        await _expansion_slots.acquire()
    finally:
        async with _expansion_lock:
            _expansion_waiting -= 1
    try:
        yield
    finally:
        _expansion_slots.release()


def _gunzip_bounded(raw: bytes) -> bytes:
    """Gunzip *raw*, refusing past the cap. **Blocking CPU, thread-safe.**

    Decompresses INCREMENTALLY with an output limit rather than calling
    ``gzip.decompress`` and measuring afterwards. That ordering is the entire
    protection: a bomb's expansion is refused while it is still a few chunks of
    output, so the process never holds the gigabyte that measuring-after would
    require it to allocate first.

    ``wbits=16 + MAX_WBITS`` selects gzip framing (a bare zlib stream is not
    accepted — the file this reads is what the export endpoint wrote).

    Raises:
        _BundleTooLarge: if the output would exceed :data:`_MAX_DECOMPRESSED_BYTES`.
        zlib.error: if *raw* is not a well-formed gzip stream.
    """
    dobj = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out: list[bytes] = []
    produced = 0
    data = raw
    while True:
        chunk = dobj.decompress(data, _CHUNK_BYTES)
        produced += len(chunk)
        if produced > _MAX_DECOMPRESSED_BYTES:
            # Refused HERE, holding one chunk past the cap and not a byte more.
            raise _BundleTooLarge(produced)
        out.append(chunk)
        if dobj.eof:
            break
        # Input zlib could not process because the output limit was hit. Empty
        # means the input ran out instead, which for a stream that has not
        # reached eof means it was truncated.
        data = dobj.unconsumed_tail
        if not data:
            break
    if not dobj.eof:
        raise zlib.error("incomplete gzip stream")
    if dobj.unused_data:
        # A second gzip member. The export endpoint writes exactly one, so a
        # concatenated file is not something this produced; refusing beats
        # decoding the first member and silently dropping the rest.
        raise zlib.error("trailing data after the gzip stream")
    return b"".join(out)


async def _read_bundle_body(
    request: web.Request, keep: contextlib.AsyncExitStack
) -> tuple[Any, web.Response | None]:
    """Read the request body as a bundle document. Returns ``(body, error)``.

    Accepts BOTH shapes the two callers actually send, distinguished by the
    body's own first two bytes:

    * **gzip** — the file ``GET /api/chat/slots/{key}/export`` hands the user,
      byte for byte. Reading these bytes as ``request.json()`` answers
      ``transfer_invalid_json``, so accepting the sniffed gzip is what lets the
      product take back the one file it produces without the user gunzipping it
      by hand first.
    * **plain JSON** — what the tunnel's server-to-server ``send_session_bundle``
      posts. The sending side is an independently-updated install, so accepting
      plain JSON keeps a peer that posts uncompressed working; demanding
      compression would break any peer that posts this shape.

    Sniffing the magic rather than branching on ``Content-Type`` is what makes
    that work: a browser uploading a ``.gz`` off disk sends whatever its platform
    guesses, and the format is not the header's to decide when the bytes say it
    plainly.

    Decompression runs off the loop — up to 60 MiB of gzip is real CPU, and this
    module already offloads its other bulk-CPU pass (``_redact_history_rows``)
    for the same reason. It is also ADMITTED rather than simply started: the
    per-body ceiling bounds one request, and the sum across concurrent requests
    is what reaches a host, so :func:`_expansion_admission` caps how many expand
    at once and this returns ``429 transfer_expansion_busy`` past the queue.

    The permit is entered on *keep*, the CALLER's stack, so it is still held when
    this returns. What the bound has to cover is how much decompressed bundle is
    RESIDENT at once, and a bundle is resident — as bytes, then as the parsed
    document — until the arrival that consumes it finishes. Releasing on return
    would leave the count of resident bundles unbounded, which is the sum this
    exists to bound. It costs throughput: a permit is now held across redaction
    and persistence, so concurrent importers reach the queue sooner. That is the
    intended trade, because the alternative bounds the CPU of expansion and not
    the memory.

    Args:
        request: the arriving request; its body is read once.
        keep: the arrival's own stack, which the expansion permit is entered on.
    """
    try:
        raw = await request.read()
    except web.HTTPRequestEntityTooLarge:
        # The one body-read failure the server can NAME. aiohttp raises this from
        # ``read()`` when the body passes the Application's ``client_max_size``,
        # so the cause is known and ``transfer_bundle_too_large`` already carries
        # the copy for it in every locale. Answering the generic code here would
        # hand a person whose file is simply too big a message that hedges
        # between that and a dropped connection, and send them looking for a
        # network fault they do not have.
        #
        # No byte figure in the reason: the ceiling that fired is the
        # Application's, which this module does not own, and the sibling
        # refusal below can quote a size only because that one IS its ceiling.
        return None, _reject(
            "request body exceeds the server's body-size limit",
            "transfer_bundle_too_large",
        )
    except Exception:
        # What is left is genuinely unattributable: a client that hung up
        # mid-upload, a malformed transfer encoding. Nothing was written; a
        # resend is safe.
        return None, _reject("could not read the request body", "transfer_body_unreadable")

    if raw[:2] == _GZIP_MAGIC:
        try:
            # Registered on the CALLER's stack, not held by an ``async with``
            # here: a decompressed bundle stays resident in parsed form through
            # redaction and persistence, so releasing the permit when this
            # function returns would bound only the CPU of expansion and leave
            # the residency it exists to bound unbounded in count.
            await keep.enter_async_context(_expansion_admission())
            raw = await asyncio.to_thread(_gunzip_bounded, raw)
        except _ExpansionBusy:
            # Retryable and the sender is at no fault, so it gets a status that
            # says so. 429 rather than 400 for the same reason the slot cap does:
            # the body was fine, the host is busy.
            return None, web.json_response(
                {
                    "error": "too many imports are being decompressed; please retry",
                    "code": "transfer_expansion_busy",
                },
                status=429,
            )
        except _BundleTooLarge:
            # A SIZE, not a byte count. This string is rendered verbatim on the
            # menu row that offered the import, so it is the only copy the person
            # who picked the file ever sees; "expands past 65 MiB" is something
            # they can check against the file, and "past 68388608 bytes" is not.
            ceiling_mib = _MAX_DECOMPRESSED_BYTES // (1024 * 1024)
            return None, _reject(
                f"compressed bundle expands past {ceiling_mib} MiB",
                "transfer_bundle_too_large",
            )
        except Exception:
            # Corrupt or truncated gzip. A DISTINCT code from bad JSON: the
            # sender needs to know its file did not survive the trip, not go
            # looking for a syntax error in a document it never wrote by hand.
            return None, _reject("could not decompress the bundle", "transfer_invalid_gzip")

    try:
        return json.loads(raw), None
    except Exception:
        return None, _reject("invalid JSON body", "transfer_invalid_json")


def _validate_bundle(body: Any) -> tuple[dict[str, Any], web.Response | None]:
    """Validate an inbound bundle. Returns ``(bundle, error_response)``."""
    if not isinstance(body, dict):
        return {}, _reject("body must be a JSON object", "transfer_body_not_object")

    version = body.get("bundle_version")
    # Reject an unknown version outright instead of best-effort parsing: see
    # BUNDLE_VERSION.
    if version not in _SUPPORTED_BUNDLE_VERSIONS:
        return {}, _reject(
            f"unsupported bundle_version {version!r} "
            f"(this instance speaks {list(_SUPPORTED_BUNDLE_VERSIONS)})",
            "transfer_version_unsupported",
        )

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list):
        return {}, _reject("messages must be an array", "transfer_messages_not_array")
    if not raw_messages:
        return {}, _reject("bundle carries no messages", "transfer_bundle_empty")
    if len(raw_messages) > _MAX_MESSAGES:
        return {}, _reject(
            f"too many messages ({len(raw_messages)} > {_MAX_MESSAGES})",
            "transfer_too_many_messages",
        )

    total = 0
    messages: list[dict[str, Any]] = []
    for i, m in enumerate(raw_messages):
        if not isinstance(m, dict):
            return {}, _reject(f"message {i} is not an object", "transfer_message_not_object")
        role = m.get("role")
        if role not in _VISIBLE_ROLES:
            return {}, _reject(
                f"message {i} has role {role!r}; expected one of {list(_VISIBLE_ROLES)}",
                "transfer_message_bad_role",
            )
        content = m.get("content", "")
        if not isinstance(content, str):
            return {}, _reject(
                f"message {i} content must be a string", "transfer_message_bad_content"
            )
        if len(content) > _MAX_CONTENT_CHARS:
            return {}, _reject(
                f"message {i} content too long ({len(content)} > {_MAX_CONTENT_CHARS})",
                "transfer_message_too_long",
            )
        total += len(content)
        if total > _MAX_TOTAL_CHARS:
            return {}, _reject(
                f"bundle too large (> {_MAX_TOTAL_CHARS} chars of content)",
                "transfer_bundle_too_large",
            )
        ts = m.get("ts", "")
        messages.append({"role": role, "content": content, "ts": ts if isinstance(ts, str) else ""})

    title = body.get("title", "")
    if not isinstance(title, str):
        return {}, _reject("title must be a string", "transfer_bad_title")
    origin = body.get("origin", "")
    if not isinstance(origin, str):
        return {}, _reject("origin must be a string", "transfer_bad_origin")
    agent = body.get("agent", "")
    if not isinstance(agent, str):
        return {}, _reject("agent must be a string", "transfer_bad_agent")

    validated: dict[str, Any] = {
        # The sender's explicit "I had context but withheld it" signal, coerced
        # because it arrives from an untrusted peer. Carried through because
        # validation normalises the body, so anything dropped here is invisible
        # downstream -- and this is what tells a degraded import apart from a
        # session that simply never had a kiro-cli context.
        "layer_b_skipped": bool(body.get("layer_b_skipped")),
        "title": title[:_MAX_TITLE_CHARS],
        "origin": origin[:_MAX_TITLE_CHARS],
        "agent": agent,
        "messages": messages,
    }

    # Layer B is optional: absent on a v1 bundle, or on a session that never had
    # a kiro-cli context. When present it must be well-formed and bounded before
    # anything is written to disk — the same untrusted-input stance as messages.
    layer_b = body.get("layer_b")
    if layer_b is not None:
        if not isinstance(layer_b, dict):
            return {}, _reject("layer_b must be an object", "transfer_layer_b_not_object")
        env = layer_b.get("envelope")
        events = layer_b.get("events")
        if not isinstance(env, dict):
            return {}, _reject(
                "layer_b.envelope must be an object", "transfer_layer_b_bad_envelope"
            )
        if not isinstance(events, str):
            return {}, _reject("layer_b.events must be a string", "transfer_layer_b_bad_events")
        if len(events) > _MAX_LAYER_B_CHARS:
            return {}, _reject(
                f"layer_b too large (> {_MAX_LAYER_B_CHARS} chars)",
                "transfer_layer_b_too_large",
            )
        validated["layer_b"] = {"envelope": env, "events": events}

    return validated, None


def _resolve_agent(name: str) -> str:
    """Return *name* if this instance has an agent by that name, else ``""``.

    An agent template is a local object; carrying a name the target does not
    have would leave the slot pointing at nothing. Resolution failure is not an
    error — the session imports onto the default agent.

    **Blocking**: ``list_agents`` scans the agents directory and parses each
    manifest, so callers on the event loop must offload it (see the call site in
    :func:`api_chat_slot_import`).
    """
    if not name:
        return ""
    try:
        if any(getattr(a, "name", "") == name for a in list_agents()):
            return name
    except Exception:
        # Discovery is best-effort: a broken agents dir must not fail an import.
        logger.debug("session_transfer: agent discovery failed", exc_info=True)
    return ""


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


async def api_chat_slot_import(request: web.Request) -> web.Response:
    """POST /api/chat/slots/import — materialise a transferred session bundle.

    Always creates a NEW slot (copy semantics, see the module docstring). The
    imported slot deliberately has no project directory: the user picks one on
    arrival.

    The SINGLE server route behind both arrival routes — a session pushed over
    the tunnel by a peer's ``send_session_bundle``, and a session installed from
    an exported file — so everything that must hold for "a session arrived here"
    belongs in this function and nowhere else. Two such rules live here: the body
    is accepted gzipped or plain (``_read_bundle_body``), and the session is filed
    under ``Imported`` / ``from <sender>`` (``arrival_folders``). Both are written
    once, for both routes, on purpose: the transport a bundle arrived by must not
    decide either how its bytes are read or where the session lands.

    Owns the stack that holds a decompressed bundle's expansion permit. The
    permit has to outlive the READ — a gzip body is still resident, in parsed
    form, through redaction and persistence — so it cannot be released inside
    ``_read_bundle_body``, and the arrival is a separate function purely so the
    permit's span is the whole arrival without re-indenting it under a block.
    """
    async with contextlib.AsyncExitStack() as keep:
        # Bound to a name rather than returned from inside the block so the
        # function has one definite exit: an AsyncExitStack's ``__aexit__`` is
        # typed as possibly SUPPRESSING, which makes a return inside the block a
        # path that can fall through it. The permit still spans the arrival —
        # the stack closes here, after the arrival has produced its response.
        response = await _install_arrived_bundle(request, keep)
    return response


async def _install_arrived_bundle(
    request: web.Request, keep: contextlib.AsyncExitStack
) -> web.Response:
    """Materialise one arrived bundle. See :func:`api_chat_slot_import`.

    *keep* holds resources that must live until the arrival is finished rather
    than until the body has been read — today that is the expansion permit.
    """
    # Imported function-locally, not at module level: chat_handlers' import graph
    # reaches back here (see the layering note at the top of this module), so a
    # module-level import would close an import cycle.
    from kiro_crew.dashboard.chat_handlers import (
        _materialise_slot_from_history,
        _redact_history_rows,
    )

    state: DashboardState = request.app["state"]
    request_app = request.get("app", "")
    caller = request_app or "dashboard"
    # The AUTHORIZATION identity, resolved by the shared rule rather than read
    # off the request the way ``request_app`` above is. The two differ for a
    # caller that carries no app claim but does carry a session key an app owns:
    # ``request.get("app")`` is empty there and the shared rule derives the app.
    # Filing must see the derived value, because that is the caller an
    # app-scoped arrival has to be refused a folder for. Kept SEPARATE from
    # ``request_app`` on purpose — that value is the slot's own app attribution
    # and its meaning is not this one's.
    folder_app = effective_request_app(state, request)

    if state.live_slot_count() >= MAX_LIVE_SLOTS:
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_import",
            outcome="denied",
            source="rate_limit",
            resources=f"slot_count={state.live_slot_count()}",
            error="slot cap reached",
        )
        return web.json_response(
            {
                "error": f"slot cap reached ({MAX_LIVE_SLOTS})",
                "code": "transfer_slot_cap",
            },
            status=429,
        )

    body, body_err = await _read_bundle_body(request, keep)
    if body_err is not None:
        return body_err

    bundle, err = _validate_bundle(body)
    if err is not None:
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_import",
            outcome="denied",
            source="dashboard",
            resources="bundle validation",
            error="bundle rejected",
        )
        return err

    messages = bundle["messages"]
    # Agent resolution scans the agents directory and parses each manifest, so it
    # cannot run on the event loop. Only pay the thread hop when a hint was
    # actually sent — the common case is an empty hint, which resolves to "" with
    # no IO at all.
    agent_hint = bundle["agent"]
    resolved_agent = await asyncio.to_thread(_resolve_agent, agent_hint) if agent_hint else ""

    # Title/origin are redacted here because they feed the marked title and the
    # audit line; the shared materialiser re-redacts the composed title via
    # ``_rehydrate_slot_title`` (idempotent). Per-MESSAGE redaction is NOT done
    # here: the receive-side rows are content-redacted just below, in one
    # off-loop ``_redact_history_rows`` pass before construction, so a second
    # pass would double the regex cost over up to _MAX_TOTAL_CHARS of peer
    # content for no persisted difference. User turns stay verbatim there,
    # matching fork.
    source_title = bundle["title"] or "Untitled"
    source_title, _ = redact_exfiltration_urls(source_title)
    source_title, _ = redact_credentials(source_title)
    origin = bundle["origin"]
    origin, _ = redact_exfiltration_urls(origin)
    origin, _ = redact_credentials(origin)
    suffix = f" (from {origin})" if origin else ""

    # Normalise the bundle turns to the row shape the materialiser hydrates from.
    # Build the receive-side rows (dict construction, no GIL-held regex), then
    # content-redact them OFF THE LOOP before construction. Redaction at the
    # transfer bounds (~20M chars) is ~1s of GIL-held regex, so it runs in a
    # thread where it yields freely and — critically — BEFORE any slot exists, so
    # a stall here is only a stall, not a window on a half-built slot. The
    # materialiser is then synchronous and does no content redaction. This is the
    # importer's own egress-mirroring scrub (defense-in-depth; it must not assume
    # the sender scrubbed).
    rows: list[dict] = [
        {"role": m["role"], "content": m["content"], "ts": m["ts"]} for m in messages
    ]
    rows = await asyncio.to_thread(_redact_history_rows, rows)

    # The marked title travels as the persisted title so the shared path restores
    # it; ``origin`` is NOT set on the metadata snapshot -- that key is the
    # sending instance's label, not a slot origin tag, and import deliberately
    # lands untagged (default origin), exactly as before. No folder_id / pinned /
    # tags travel: the bundle's own folder_id is a reference into the SENDER's
    # tree and is dropped, and ``folder_id`` is deliberately absent HERE so
    # placement is resolved after the slot exists (see the filing call below) --
    # a folder created in front of the post-await slot-cap re-check is left
    # behind when that check answers 429.
    meta = {
        "title": f"{_IMPORT_TITLE_MARKER}{source_title}{suffix}",
        "agent": resolved_agent,
    }

    # Re-check the cap HERE, with no await between this test and the creation
    # inside the shared materialiser below. The check at the top of the handler
    # is necessary but not sufficient: body parsing and agent resolution await,
    # so N concurrent imports near the cap all clear that first test before any
    # of them allocates, and all N are admitted.
    # This second test closes that window because the loop cannot switch tasks
    # between it and the ``get_or_create_slot`` inside ``_materialise_slot_from_history``.
    #
    # Distinct from the construction accounting the materialiser opens: that keeps
    # a RETRACTED slot counted, which is a different window (after creation). Both
    # are needed.
    if state.live_slot_count() >= MAX_LIVE_SLOTS:
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_import",
            outcome="denied",
            source="rate_limit",
            resources=f"slot_count={state.live_slot_count()}",
            error="slot cap reached (post-await recheck)",
        )
        return web.json_response(
            {
                "error": f"slot cap reached ({MAX_LIVE_SLOTS})",
                "code": "transfer_slot_cap",
            },
            status=429,
        )

    # The shared materialiser mints the key (name=None), registers the slot and
    # holds it under ``begin_slot_construction`` for the duration of hydration,
    # then hands it back registered and counted. ``serialize_slots`` omits an
    # under-construction slot, so Layer B, the durable save and the publish all
    # run below while the slot is hidden from clients (though registered, so a
    # concurrent same-key lookup still resolves it) -- it is shown only when this
    # handler ends construction and pushes at its tail.
    slot = _materialise_slot_from_history(
        state,
        name=None,
        history_key="",
        meta=meta,
        all_messages=rows,
        app=request_app,
        # Every row exists only in memory and is persisted by the save below, so
        # none is "older on disk": surface all of them and leave _disk_older_count
        # at 0 rather than claiming a frozen prefix that was never written.
        window_limit=None,
        # Import synthesised its metadata; it read no transcript off disk, so the
        # delete-won disk-identity guard must stay dormant.
        disk_meta_observed=False,
        # Silent replay of a bundle onto a slot that stays registered and under
        # construction throughout its synchronous hydration (it is retracted from
        # ``_slots`` only afterwards, for the async tail below): broadcasting each
        # row would push an under-construction slot's peer content to every client
        # and retire live question cards.
        broadcast_rows=False,
        # Bundle rows carry no message id; mint one, or the imported rows land
        # permanently id-less and drop out of mid-keyed features.
        mint_missing_mids=True,
    )
    sm_key = effective_session_key(slot)
    sessions = getattr(state, "sessions", None)
    # Retract the slot from ``_slots`` for the async finalization tail below.
    # The materialiser's hydrate loop is synchronous, but Layer B write/join and
    # the durable save that follow AWAIT while the slot would otherwise be
    # registered -- and the raw ``state._slots.get(name)`` acquirers (delete/close,
    # regenerate, rewind) bypass the ``get_slot``/``get_or_create_slot`` guards, so
    # a crafted request against the minted key could close, resurrect or truncate
    # the slot mid-finalization. Popping it closes EVERY such acquirer at once:
    # the slot is not in ``_slots`` to be found. This is safe here where it was
    # NOT for resume: import MINTS its key from the monotonic counter and does not
    # return it until this handler responds, so no concurrent request can target
    # it -- there is no second caller to mint a duplicate the way a client-supplied
    # resume key allowed. The slot stays under ``begin_slot_construction`` (the
    # count is released in the finally) and is re-registered on the success path
    # below, after finalization lands. Every error path already pops it (a no-op
    # now) and rolls back the join.
    state._slots.pop(slot.key, None)

    # Resume mode, reported back to the sender so a degraded copy is never shown
    # as a full one. "prefix" is correct for a v1/no-Layer-B bundle: the session
    # opens on the transcript, which is exactly what was sent.
    resume_mode = "prefix"
    layer_b_sid = ""
    # Beside ``layer_b_sid`` and for the same reason: the except arms below read
    # it, and a failure BEFORE the filing call must find an empty tuple rather
    # than an unbound name.
    created_folders: tuple[str, ...] = ()
    # The same rows plus the record the resolver WROTE for each, which is what
    # lets the rollback tell a row still holding what the import created from one
    # a person has since renamed, recoloured or moved. Empty deletes nothing.
    created_rows: tuple[tuple[str, str, str], ...] = ()
    # Adopted rows the filing found HIDDEN. The un-hide is deferred to the
    # landed path because no rollback can put the flag back on an adopted row.
    hidden_rows: tuple[str, ...] = ()
    layer_b = bundle.get("layer_b")

    try:
        if layer_b:
            # Files in a thread (blocking IO), join on the loop (the live map's
            # whole-file write is unsynchronised against concurrent session
            # starts). See _write_layer_b_files / _join_layer_b.
            written_sid = await asyncio.to_thread(_write_layer_b_files, layer_b, slot.agent)
            layer_b_sid = written_sid or ""
            resumable = bool(layer_b_sid) and _join_layer_b(sessions, sm_key, layer_b_sid)
            if not resumable:
                logger.info(
                    "session_transfer: imported %s without Layer B; "
                    "it will resume via the transcript prefix",
                    slot.key,
                )
                if layer_b_sid:
                    await asyncio.to_thread(_unlink_layer_b_files, layer_b_sid)
                    layer_b_sid = ""
            resume_mode = "session_load" if resumable else "prefix"

        # Mark the IMPORTED TAB when it arrived without resumable context. The
        # sender's row is gone the moment its menu closes; the tab title is the
        # one surface that persists and is present where the loss will be felt.
        # Fires when the sender either SENT context that failed to land or told
        # us it deliberately withheld context (``layer_b_skipped``, a mid-turn
        # source); never for a bundle that simply never had a kiro-cli context.
        if resume_mode == "prefix" and (bundle.get("layer_b") or bundle.get("layer_b_skipped")):
            slot.title = f"{slot.title} — transcript only"

        # ARRIVAL PROVENANCE FILING (docs/request-for-change/rfc-arrival-provenance-filing.md).
        # Here rather than in ``meta`` above for two reasons, both load-bearing.
        #
        # The slot already exists, so the post-await slot-cap re-check above
        # cannot answer 429 from here on -- a folder write standing in front of
        # that check is left behind when it fires, which is folder-store
        # exhaustion with a narrower trigger than the loop an app token would
        # otherwise run.
        #
        # And it is the LAST await before the durable save, so the window in which
        # a delete can invalidate the placement is as short as this handler can
        # make it. The re-check below closes what is left of that window, and the
        # repair after re-registration closes the rest: for this whole stretch the
        # slot is retracted from ``state._slots``, which is the mapping the folder
        # delete handler's unfile sweep iterates, so a delete landing here cannot
        # see the session to unfile it.
        #
        # Best-effort by contract: ``arrival_folder_id`` answers "" for every
        # refusal (app-scoped caller, ceiling reached, store write failure) and
        # the session then lands unfiled, exactly as it did before this shipped.
        # The transcript is the payload; the grouping is convenience.
        filing = await arrival_folder_id(state, origin=origin, request_app=folder_app)
        arrival_folder = filing.folder_id
        # Rows this filing CREATED, for the failure paths below. Held in the
        # handler's own scope rather than re-derived: after a failure the store no
        # longer says which rows were new, and an adopted row must survive.
        created_folders = filing.created_ids
        created_rows = filing.created_rows
        hidden_rows = filing.hidden_ids
        if arrival_folder and await arrival_folder_exists(state, arrival_folder):
            slot.folder_id = arrival_folder

        # best_effort=False: a swallowed write failure would answer 200 while the
        # imported session exists only in memory, so the peer believes the
        # transfer landed and a restart before the next flush loses it. An import
        # that cannot be persisted must fail loudly instead.
        try:
            await save_slot_off_loop(state, slot, best_effort=False)
        except Exception:
            # Retryable, peer at no fault: coded answer, source untouched, resend
            # is safe. Drop the registered-but-hidden slot and release its
            # construction count in the finally; it was never shown (the
            # construction filter hid it), so no broadcast is owed. Undo the join
            # written above.
            state._slots.pop(slot.key, None)
            sid = _forget_layer_b_join(sessions, sm_key) or layer_b_sid
            if sid:
                await asyncio.to_thread(_unlink_layer_b_files, sid)
            # The folder was committed before this save, so without this the
            # failed import leaves an empty row behind. Placed after the pop, so
            # the importing slot is already out of the mapping the rollback reads
            # and does not count as a session filed into the row.
            await discard_arrival_folders(state, created_rows)
            logger.warning(
                "session_transfer: could not persist imported slot=%s; refusing the import",
                slot.key,
                exc_info=True,
            )
            sel().log_api_access(
                caller=caller,
                operation="chat.slot_import",
                outcome="error",
                source="dashboard",
                resources=f"to={slot.key}",
                error="durable save failed",
            )
            return web.json_response(
                {
                    "error": "could not persist the imported session; please retry",
                    "code": "transfer_import_save_failed",
                },
                status=503,
            )
    except asyncio.CancelledError:
        # CancelledError is a BaseException, so ``except Exception`` never sees
        # it: a shutdown or client disconnect after the join left an orphaned
        # map entry plus its files. Roll back synchronously (awaiting inside a
        # cancelled task is not dependable), then re-raise so cancellation
        # propagates.
        state._slots.pop(slot.key, None)
        try:
            sid = _forget_layer_b_join(sessions, sm_key)
            if sid:
                _unlink_layer_b_files(sid)
        except Exception:
            logger.debug("session_transfer: cancellation rollback failed", exc_info=True)
        # No folder rollback here, deliberately. ``discard_arrival_folders`` is
        # async because the folder store's lock is, and this arm is synchronous
        # for the reason stated above. Scheduling it as a task would be
        # dependable only for a disconnect and not for a shutdown, so it would
        # trade a plain gap for one that looks closed. A cancellation mid-import
        # can therefore still leave an empty row, which the row's own visibility
        # makes recoverable by hand.
        raise
    except Exception:
        # The slot was hidden by the construction filter throughout, so nothing
        # is visible to retract -- but the join and its files may exist. Undo
        # them; the finally releases the construction count and the pop drops the
        # hidden slot.
        state._slots.pop(slot.key, None)
        try:
            sid = _forget_layer_b_join(sessions, sm_key)
            if sid:
                await asyncio.to_thread(_unlink_layer_b_files, sid)
        except Exception:
            logger.debug("session_transfer: join rollback failed", exc_info=True)
        # Same reason as the durable-save arm: a folder committed before the
        # failure is this import's to unwind. Outside the try above so a join
        # rollback failure cannot skip it.
        await discard_arrival_folders(state, created_rows)
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_import",
            outcome="error",
            source="dashboard",
            resources=f"to={slot.key}",
            error="import finalisation failed",
        )
        raise
    finally:
        # Every exit releases the construction count the shared materialiser
        # opened: the 503 return above, the raises here, and the success path.
        # Runs before the re-registration below, but nothing between them awaits,
        # so no coroutine can observe the slot as neither published nor counted.
        state.end_slot_construction(slot.key)

    sel().log_api_access(
        caller=caller,
        operation="chat.slot_import",
        outcome="allowed",
        source="dashboard",
        resources=(
            f"to={slot.key},messages={len(messages)},"
            f"origin={origin or 'unknown'},agent={slot.agent or 'default'},"
            f"resume={resume_mode}"
        ),
    )
    # Everything is in place -- transcript persisted, Layer B joined. The slot
    # was RETRACTED from ``_slots`` for the async finalization tail above (so no
    # raw acquirer could reach it mid-finalization); this re-registers it now that
    # it is a complete, resumable session. The finally above ended construction,
    # so this push is the first frame a client sees and it shows a fully
    # materialised session.
    state._slots[slot.key] = slot

    # ARRIVAL FILING REPAIR. The placement above was checked while the slot was
    # RETRACTED from ``_slots``, and the folder delete handler's unfile sweep
    # iterates exactly that mapping -- so a delete committing during the durable
    # save could not reach this session and left it pointing at a row that no
    # longer exists. Re-checking HERE, after re-registration, is what closes
    # that: from this line on the sweep can see the slot, so this is the last
    # moment a delete can be missed. A gone folder is cleared and re-saved,
    # which renders the session at the top level -- what an unfiled arrival
    # always did -- rather than at a dangling id no later folder operation
    # corrects. That re-save is best-effort about a LOCK (a timeout marks the
    # slot dirty for the periodic flush) but NOT about a concurrent delete: see
    # the refusal branch below, which rolls the import back rather than
    # reporting it landed. Before the push below, so the first frame a client
    # sees carries the repaired placement rather than one it has to be
    # corrected out of.
    async def _refuse_as_deleted(witness: str) -> web.Response:
        """Roll the import back and answer a coded failure, never ``ok``.

        Shared by both refusal paths below so they cannot drift: whichever
        witness fired, the session is gone and exactly the same unwinding is
        owed — drop the slot, undo the Layer B join, remove its files (those
        helpers are local to this module, so the permanent delete does not
        unwind them). *witness* names which one fired, for the log only; the
        wire answer is identical because the caller's situation is identical.
        """
        # WHAT MAY THIS REFUSAL CLAIM? The transcript was persisted BEFORE this
        # point, and the witness above collapses three outcomes into "deleted":
        # the file is gone, the file belongs to a NEW incarnation, and existence
        # is unverifiable. Only the first lets this answer say nothing was kept.
        #
        # The other two leave a file on disk that must NOT be unlinked here -- a
        # new incarnation is somebody else's session, and an unverifiable read
        # names nothing that can safely be removed -- so the honest answer
        # discloses the leftover instead of asserting a clean slate. Retryable
        # either way; only the promise differs. Read BEFORE the unwinding below,
        # so it reports the disk as the refusal found it.
        remains = await asyncio.to_thread(session_transcript_remains, state, slot)
        # KEY-SCOPED CLEANUP NEEDS AN IDENTITY GUARD, for the same reason the
        # file above is left alone: the witness fires when this slot's session was
        # deleted, and a replacement can land at the SAME key while this tail
        # runs. Popping by key alone would then drop the replacement's slot, and
        # forgetting the join by key alone would take its mapping and its files.
        # Compare the OBJECT: only this import's own slot is this object, so a
        # replacement is left exactly as its own writer left it.
        #
        # THREE OUTCOMES, NOT TWO. ``dict.get`` answers ``None`` for an ABSENT key
        # exactly as it does for a REPLACED one, and only the replaced case must be
        # left alone. Absent is what the ORDINARY permanent delete produces -- it
        # pops by key and puts nothing back -- and there this import's own Layer B
        # pair is nobody else's, while the delete does not unwind these
        # module-local helpers (see this function's docstring). Folding absent into
        # replaced orphans a ``.json``, a ``.jsonl`` and a join for a session with
        # no tab, and nothing re-cleans it: this return is terminal and re-arms
        # nothing.
        current = state._slots.get(slot.key)
        if current is slot:
            state._slots.pop(slot.key, None)
            sid = _forget_layer_b_join(sessions, sm_key) or layer_b_sid
            if sid:
                await asyncio.to_thread(_unlink_layer_b_files, sid)
        elif current is None:
            # Nothing to pop, and the cleanup the delete does not do is owed here.
            #
            # Scoped to ``layer_b_sid``, this import's OWN sid, rather than to the
            # sid the join reports: in the narrower case where a replacement
            # landed and was itself popped, the mapping at ``sm_key`` belongs to
            # that replacement. Unlinking the sid it names would delete that
            # session's files, and forgetting it would drop its mapping and its
            # continuable mark -- the harm the object comparison above prevents
            # for the REPLACED case, which the ABSENT case cannot inherit from it
            # because ``dict.get`` answers alike for both.
            #
            # So the join is dropped only while it still NAMES this import's own
            # sid. ``resumable_sid`` and ``forget_conversation`` both resolve
            # ``_session_map.get`` on the same folded key, so the guard reads the
            # exact value the forget would report and delete, and both are
            # synchronous with no await between them, so nothing interleaves on
            # the loop. A foreign mapping is left to its own writer, and an
            # import holding no Layer B of its own drops nothing.
            if layer_b_sid and _resolve_layer_b_sid(sessions, sm_key) == layer_b_sid:
                _forget_layer_b_join(sessions, sm_key)
            if layer_b_sid:
                await asyncio.to_thread(_unlink_layer_b_files, layer_b_sid)
        else:
            logger.warning(
                "session_transfer: slot=%s was replaced during import; leaving the "
                "replacement's slot, join and files untouched",
                slot.key,
            )
        # Covers BOTH witnesses by sitting in the shared path: the session is
        # gone, so a folder this import created for it has nothing left to hold.
        # After the pop, so the importing slot is already out of the mapping the
        # rollback reads and does not count as a session filed into the row.
        await discard_arrival_folders(state, created_rows)
        logger.warning(
            "session_transfer: slot=%s was permanently deleted during import "
            "(%s); refusing to report the transfer as landed",
            slot.key,
            witness,
        )
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_import",
            outcome="error",
            source="dashboard",
            resources=f"to={slot.key},transcript_remains={remains}",
            error="session deleted during import",
        )
        if remains:
            return web.json_response(
                {
                    "error": "the imported session was deleted while it was "
                    "being installed, and a transcript file remains on disk "
                    "that this instance cannot safely remove; please retry",
                    "code": "transfer_import_deleted_partial",
                },
                status=409,
            )
        return web.json_response(
            {
                "error": "the imported session was deleted while it was "
                "being installed; nothing was kept",
                "code": "transfer_import_deleted",
            },
            status=409,
        )

    if slot.folder_id and not await arrival_folder_exists(state, slot.folder_id):
        logger.info(
            "session_transfer: arrival folder %s went away during import of %s; "
            "leaving the session unfiled",
            slot.folder_id,
            slot.key,
        )
        slot.folder_id = ""
        # THE REPAIR MAY ONLY WRITE A SLOT IT STILL OWNS. The existence check
        # above awaits, and a close landing inside that await pops the slot and
        # THEN persists ``closed=True`` (``chat_handlers`` pops first, then saves
        # with the flag). This object's in-memory ``closed`` is still False, so an
        # unguarded repair save writes that flag back OFF: the archived record
        # loses the dismissal and the tab the person closed resurfaces.
        #
        # PRESENT, AND THIS OBJECT -- the opposite polarity to
        # ``chat_handlers._slot_still_ours``, which counts an ABSENT key as still
        # ours because a close pops before its own teardown steps. Here an absent
        # key is precisely the close this must yield to, so that helper cannot
        # decide it. Same test as the refusal path's guard above.
        #
        # Skipping rather than refusing, because the import DID land: the
        # transcript is persisted, and a close is the person's own later action on
        # a session that arrived. What the skip leaves behind is a dangling
        # ``folder_id`` on the archived record, which is a state the folder delete
        # handler already documents as "ignored on the next load".
        if state._slots.get(slot.key) is slot:
            # A REFUSAL IS NOT A COMMIT. ``save_slot_off_loop`` converts an
            # exception to ``True`` under ``best_effort`` (the slot is marked
            # dirty and the periodic flush retries), but it returns ``False``
            # CLEANLY for two cases, and they need opposite answers.
            #
            # PINNED WITH ``expected_slot_name``, because the identity check above
            # is synchronous and this save is not: the executor wait frees the
            # event loop, so a close landing in that window pops the slot and
            # persists ``closed=True`` while this call still holds
            # ``closed=False`` in memory. Without the pin the commit-boundary
            # recheck in ``chat_persistence`` is skipped entirely, and the stale
            # snapshot lands on top of the dismissal -- the tab the person closed
            # comes back. The pin makes that check atomic with the write.
            #
            # WHICH ``False`` IS IT: the delete-won guard, or the pin? Re-read the
            # map, which is synchronous and cannot race here. A map that does not
            # hold THIS slot means the pin refused, so a close or replacement won
            # -- the import landed and a close is the person's own later action, so
            # skip exactly as the branch below does. A map that still holds it
            # means the delete-won guard fired, which is terminal: nothing re-arms
            # ``_dirty``, so publishing would answer ``ok: true`` for a session
            # whose file is gone and leave the slot published as a zombie.
            if not await save_slot_off_loop(state, slot, force=True, expected_slot_name=slot.key):
                if state._slots.get(slot.key) is not slot:
                    logger.warning(
                        "session_transfer: slot=%s was replaced at the repair "
                        "save's commit boundary; skipping the filing repair so a "
                        "concurrent close is not overwritten",
                        slot.key,
                    )
                else:
                    return await _refuse_as_deleted("the repair save met the delete-won guard")
        else:
            logger.warning(
                "session_transfer: slot=%s left _slots during the arrival-folder "
                "check; skipping the filing repair so a concurrent close is not "
                "overwritten",
                slot.key,
            )

    # THE ROW THIS ARRIVAL SHARES IS RECORDED HERE, immediately above the final
    # witness. A filing that ADOPTED its destination has to leave something behind
    # for the rollback of whichever import CREATED that row: an archived session
    # is invisible to the live-slot occupancy check, so without a mark that
    # rollback would delete a placement this session still points at.
    #
    # Written on this path rather than in the resolver, and that is the whole
    # point of the placement: an adoption that never became a session needs no
    # protection, and a mark the resolver wrote could not be taken back when the
    # import failed -- two concurrent same-origin imports both failing leave each
    # other's rows marked and unreclaimable for good.
    #
    # ABOVE the witness, because this call is the last await on the path and a
    # ``DELETE /api/sessions/{key}`` can land inside it. Below the witness it
    # would yield the loop past the last check, so that delete removes the
    # transcript and pops the slot and the handler still publishes ``200 ok``,
    # with nothing downstream to correct it -- the identical window the witness
    # exists to close, reopened by being one line later. Above it, the same delete
    # is caught and the request refuses. A second witness below this call buys the
    # same guarantee and costs either an extra ``stat`` on every import that
    # adopted nothing, or a conditional witness, which is the case analysis the
    # heading below refuses.
    #
    # The cost of that ordering is a refusal that can follow the mark: a delete
    # landing in this await leaves the row marked while the import gives up, so
    # the creating import's rollback can never reclaim it. That is one visible,
    # deletable sidebar row, and only when the creating import ALSO failed -- the
    # next arrival from that origin adopts the row instead of making another. It
    # cannot be unwound here, because taking a mark back needs each import's own
    # claim recorded on the row, and one import's failure would then strip
    # another's.
    #
    # Only when the destination was adopted. A row THIS import created is in
    # ``created_folders``, and no other import holds those ids, so no other
    # rollback can reach them. Destination only: an adopted parent keeps a
    # surviving child, which the rollback's second guard already spares.
    adopted_destination = bool(slot.folder_id) and slot.folder_id not in created_folders
    if adopted_destination or hidden_rows:
        if not await mark_arrival_folder_shared(
            state,
            slot.folder_id if adopted_destination else "",
            unhide=hidden_rows,
        ):
            # THE MARK IS THE ONLY THING SPARING AN ADOPTED ROW once this session
            # archives: the rollback's occupancy guard reads LIVE slots, and an
            # archived session is popped out of that mapping. So an unrecorded
            # mark means a concurrent creator's rollback can reclaim the row while
            # this transcript still points at it, and the person is left with a
            # session filed into a folder that is gone.
            #
            # Unfiling instead, which is the state the folder-gone repair above
            # already produces and which the folder delete handler documents as
            # "a dangling id can legitimately exist" -- except this makes it true
            # rather than merely tolerated, because the id is cleared and
            # persisted rather than left dangling. Same shape as that repair,
            # deliberately: the slot-identity guard so a concurrent close is not
            # overwritten, and the delete-won ``False`` treated as terminal.
            if state._slots.get(slot.key) is slot:
                slot.folder_id = ""
                # Pinned and discriminated exactly as the repair save above, and
                # for the same reason: the identity check is synchronous, this save
                # is not, and a close landing in the gap would otherwise have its
                # ``closed=True`` overwritten by this call's stale ``closed=False``.
                if not await save_slot_off_loop(
                    state, slot, force=True, expected_slot_name=slot.key
                ):
                    if state._slots.get(slot.key) is not slot:
                        logger.warning(
                            "session_transfer: slot=%s was replaced at the "
                            "unfiling save's commit boundary; skipping so a "
                            "concurrent close is not overwritten",
                            slot.key,
                        )
                    else:
                        return await _refuse_as_deleted(
                            "the unfiling save met the delete-won guard"
                        )
            else:
                logger.warning(
                    "session_transfer: slot=%s left _slots before the shared-row "
                    "mark could be recorded; skipping the unfiling so a "
                    "concurrent close is not overwritten",
                    slot.key,
                )

    # ONE WITNESS ON EVERY PATH TO SUCCESS, AND NO AWAIT BELOW IT. The guard above
    # only fires when the arrival FOLDER went away, so on its own it leaves the
    # common case unchecked: a ``DELETE /api/sessions/{key}`` landing in the
    # folder-existence await removes this transcript and pops the slot while the
    # folder it pointed at is still perfectly fine, so the branch is skipped and
    # the handler would answer ``200 ok`` for data that has already been
    # destroyed. Nothing downstream corrects that -- the success return does not
    # re-arm ``_dirty`` and the delete's pop is terminal -- so the misleading
    # ``ok`` is the permanent record.
    #
    # The second half of that heading carries as much weight as the first, and is
    # why the arrival-row mark sits above: any await between this check and the
    # response reopens the very window the check closes, because the delete lands
    # inside that await and the check has already passed.
    # ``test_a_delete_landing_in_the_shared_row_mark_refuses`` is what keeps an
    # await from drifting back below it.
    #
    # Unconditional rather than an ``elif``, which would be the cheaper shape and
    # the wrong one: a successful ``save_slot_off_loop`` does NOT imply the
    # delete-won guard reached a decision, because ``best_effort`` converts a
    # raising save to ``True``. Checking every time makes "no path reaches ``ok``
    # without passing the witness" true by structure instead of by case analysis,
    # and ``test_the_witness_still_runs_when_the_repair_save_reported_success``
    # is what keeps that shape from being quietly narrowed back to an ``elif``.
    # The cost is one extra ``stat`` per import, which a request already bounded
    # by the live-slot cap can carry.
    #
    # ``session_was_deleted`` is the module's own witness -- already used twice on
    # the EXPORT path here, and its docstring names this caller class: one that
    # republishes a slot's content and so cannot rely on observing the guard's
    # ``False``, because the periodic flush can reach the guard first and clear
    # ``_dirty``. Off the loop because it stats and reads metadata; the export
    # sites call it bare only because the whole builder already runs in a thread.
    if await asyncio.to_thread(session_was_deleted, state, slot):
        return await _refuse_as_deleted("the delete witness fired after the finalization tail")

    _sync_dashboard_slots(state)
    state.push_slots_update()
    return web.json_response(
        {
            "ok": True,
            "key": slot.key,
            "title": slot.title,
            "messages": len(messages),
            # Resume fidelity, so the SENDER can say "Sent" vs "Sent (transcript
            # only)" instead of showing the same green row either way.
            "resume_mode": resume_mode,
        }
    )
