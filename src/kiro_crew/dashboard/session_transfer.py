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
import importlib as _importlib
import logging
import sys as _sys
from typing import TYPE_CHECKING, Any

from kiro_crew import __version__, _facade_mirror, platform_compat  # noqa: F401
from kiro_crew.agent_discovery import list_agents  # noqa: F401
from kiro_crew.atomic_write import atomic_write, fsync_dir, replace_with_retry  # noqa: F401
from kiro_crew.config.paths import data_home, kiro_sessions_dir  # noqa: F401

# The import path's layering note (why chat_handlers is imported function-locally)
# is in ``session_transfer_install``, beside the code it governs.
from kiro_crew.dashboard.arrival_folders import (  # noqa: F401
    arrival_folder_exists,
    arrival_folder_id,
    discard_arrival_folders,
    mark_arrival_folder_shared,
)
from kiro_crew.dashboard.chat_persistence import (  # noqa: F401
    _build_message_entry_uncached,
    save_slot_off_loop,
    session_transcript_remains,
    session_was_deleted,
)
from kiro_crew.dashboard.chat_utils import (  # noqa: F401
    _sync_dashboard_slots,
    effective_session_key,
    slot_history_key,
)
from kiro_crew.dashboard.session_transfer_layers import (
    _iso_now,
    _read_chained_history,
    _read_layer_b,
    _resolve_layer_b_sid,
)
from kiro_crew.dashboard.session_transfer_staging import (
    _IMPORT_TITLE_MARKER,
    _VISIBLE_ROLES,
    BUNDLE_VERSION,
    TransferBundle,
    release_bundle_files,
)
from kiro_crew.dashboard.state import MAX_LIVE_SLOTS, DashboardState, _ChatSlot  # noqa: F401
from kiro_crew.dashboard.token_auth import effective_request_app  # noqa: F401
from kiro_crew.dashboard.transcript_snapshot import (
    TRANSFER,
    SlotView,
    SnapshotUnstable,
    read_consistent_transcript,
)
from kiro_crew.history import (  # noqa: F401 - re-exported to the bundle's callers
    TranscriptBusy,
    TranscriptWithheld,
    mint_row_mid,
    monotonic_transcript_ts,
)
from kiro_crew.instances.constants import (  # noqa: F401
    DEFAULT_SESSION_TRANSFER_TIMEOUT_SECS,
    SESSION_IMPORT_MEMORY_WAIT_SECS,
)
from kiro_crew.security import (
    redact_credentials,
    redact_exfiltration_urls,
    redact_local_paths,
)
from kiro_crew.sel import sel  # noqa: F401
from kiro_crew.warm_task_brief_contract import is_warm_task_brief_message

logger = logging.getLogger("kiro_crew.dashboard.session_transfer")


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
) -> TransferBundle:
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
    the snapshot retries against the new state (the attempts, and the rules this
    builder takes them under, are ``transcript_snapshot.TRANSFER``'s). Messages
    arriving during the await are
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

    async def _flush() -> None:
        # Persist a dirty slot BEFORE every read. The tail slice only sees messages
        # at or past the boundary, so an edit made IN PLACE below it — a variant
        # switch replacing an already-persisted assistant turn — is invisible to
        # it, and if that edit's own save failed, disk still holds the previous
        # response. Flushing is safe because the save advances ``_disk_window_len``
        # itself, so afterwards the tail slice is empty and the bundle comes wholly
        # from disk; slicing on ``_resumed_count`` instead would duplicate the tail,
        # because the save does NOT touch that counter.
        #
        # best_effort=False: a swallowed failure would put us right back to bundling
        # a stale transcript, so an unpersistable source fails the transfer instead.
        # The source is otherwise untouched — a flush persists what is already in
        # memory, it does not change the conversation.
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
            # Delete-won: the session was permanently deleted while the flush
            # awaited the lock. Bundling would ship the destroyed conversation to
            # the peer (or an empty shell of it), so the transfer fails instead of
            # answering success.
            logger.warning(
                "session_transfer: slot=%s was permanently deleted during "
                "the pre-bundle flush; refusing the transfer",
                slot.key,
            )
            raise SnapshotUnstable("the session was permanently deleted")

    async def _read(view: SlotView) -> TransferBundle:
        # Everything the bundle takes from the slot is captured HERE, on the loop,
        # in the same breath as the tail the view holds, so the thread below never
        # touches the slot while the loop could be appending to it. A retry happens
        # because the slot CHANGED, so a record taken once up front could describe
        # a model or a policy the shipped transcript never ran under.
        title = slot.title if slot._titled else ""
        agent = slot.agent
        # The SESSION key is the exception and is passed in pinned. The transcript
        # key was fixed before the flush, so the session the shipped turns ran on
        # is already decided; re-resolving it here would let a rebind landing in
        # the flush await pair this transcript with another session's approval
        # policy.
        source = _snapshot_source_record(state, slot, sm_key) if with_source else None
        # Layer B eligibility is decided per attempt too. Layer A records the user's
        # prompt as soon as it is submitted, but kiro-cli only writes Layer B when
        # the turn persists -- so a mid-turn bundle pairs a transcript that SHOWS
        # the prompt with a context that does not contain it, and the peer's
        # ``session/load`` would resume the model behind its own visible
        # transcript. Degrading to transcript-only is the honest outcome and is
        # plumbed end to end -- the import reports ``resume_mode: prefix`` and the
        # sender's row reads "Sent (transcript only)". A value computed before the
        # first attempt would let a retry pick up a prompt that started during the
        # read in Layer A while still shipping the pre-turn Layer B.
        mid_turn = bool(getattr(slot, "running", False))
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
            # Resolved on the loop: the lookup self-prunes the session map, so it
            # cannot go into the worker thread (see _resolve_layer_b_sid). The
            # thread receives only an immutable string.
            layer_b_sid = _resolve_layer_b_sid(getattr(state, "sessions", None), sm_key)
            layer_b_withheld = False
        # Read AND assemble off the loop. Assembly redacts every assistant turn,
        # and the transcript can run to the bundle cap, so those regex scans are
        # far too much CPU to hold the loop with — the same starvation that
        # exits the gateway via LoopStallWatchdog.
        return await asyncio.to_thread(
            _read_and_assemble,
            state,
            key,
            # Never None here: TRANSFER refuses a boundary ahead of the window.
            view.tail or [],
            title,
            agent,
            origin,
            layer_b_sid,
            layer_b_withheld,
            source,
        )

    # A bundle the snapshot does not return carries a Layer B snapshot nothing else
    # will remove, so every refusal and retry after the read releases it.
    snapshot = await read_consistent_transcript(
        state, slot, TRANSFER, _read, persist=_flush, discard=release_bundle_files
    )
    return snapshot.result


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
) -> TransferBundle:
    """Read the transcript + Layer B and assemble the bundle. **Runs in a thread.**

    Touches no slot state and no session map — *tail*, *title*, *agent*,
    *layer_b_sid* and *source* are all snapshots the caller took on the event
    loop — so it is safe off-loop. Only the file reads happen here.
    """
    history, publication_keys = _read_chained_history(state, session_key)
    history.extend(tail)
    layer_b = _read_layer_b(layer_b_sid)
    if layer_b_sid and layer_b is None:
        # A sid was MAPPED but its files would not read -- pruned or unparseable
        # JSONL. That is context this session genuinely had and is now giving up,
        # which is the sender's other degradation case: the
        # peer must be told, or the receiving tab shows a full-looking copy with
        # no resumable context behind it. Distinct from ``layer_b_sid == ""``,
        # which means there was never a context to carry.
        layer_b_skipped = True
    try:
        payload = _assemble_bundle(history, title, agent, origin, layer_b, layer_b_skipped, source)
        return TransferBundle(payload, publication_keys=publication_keys)
    except BaseException:
        release_bundle_files({"layer_b": layer_b})
        raise


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
        if is_warm_task_brief_message(m):
            continue
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


# --------------------------------------------------------------------------- #
# This module is the facade of the session transfer: every name is reached as
# ``kiro_crew.dashboard.session_transfer.<name>``. It keeps the sending side's
# bundle assembly, where the outbound redaction runs. A read of a name an owner
# binds is served by that owner, and a write -- ``monkeypatch.setattr``,
# ``mock.patch`` -- reaches every member holding it (:mod:`kiro_crew._facade_mirror`).
# Owners never import this module. New work goes to the owner of its
# responsibility:
#
#   ``session_transfer_staging`` -- the bundle format, staging dirs and bundle files
#   ``session_transfer_layers`` -- reading Layer A and B, writing Layer B on arrival
#   ``session_transfer_intake`` -- admitting an arriving body and parsing it
#   ``session_transfer_install`` -- validating and installing an arrived bundle
# --------------------------------------------------------------------------- #
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.session_transfer_staging",
    "kiro_crew.dashboard.session_transfer_layers",
    "kiro_crew.dashboard.session_transfer_intake",
    "kiro_crew.dashboard.session_transfer_install",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.session_transfer_install import (  # noqa: F401
        _IMPORT_WINDOW,
        _build_redacted_rows,
        _install_arrived_bundle,
        _prefix_row,
        _PrefixPublication,
        _remove_after_save,
        _resolve_agent,
        _validate_bundle,
        _write_import_prefix,
        api_chat_slot_import,
    )
    from kiro_crew.dashboard.session_transfer_intake import (  # noqa: F401
        _DISK_CHECK_EVERY_CHUNKS,
        _DISK_HEADROOM_BYTES,
        _GZIP_MAGIC,
        _JSON_STRING,
        _MAX_CONCURRENT_EXPANSIONS,
        _MAX_CONCURRENT_UPLOADS,
        _MAX_QUEUED_EXPANSIONS,
        _MEMORY_HEADROOM_BYTES,
        _MEMORY_POLL_SECS,
        _MESSAGE_MARKER,
        _NON_BMP_LEAD,
        _PARSE_MEMORY_FACTOR,
        _PER_MESSAGE_BYTES,
        _PER_VALUE_BYTES,
        _STRUCTURAL_MARKS,
        _SURROGATE_ESCAPE,
        _WIDE_ESCAPE,
        _WIDE_LEAD,
        _cgroup_memory_bounds,
        _disk_full_response,
        _disk_inflight,
        _DiskFull,
        _expansion_admission,
        _expansion_lock,
        _expansion_slots,
        _expansion_waiting,
        _ExpansionBusy,
        _free_bytes,
        _gateway_memory,
        _gunzip_file,
        _load_json_file,
        _measure_document,
        _memory_admission,
        _memory_reserved,
        _MemoryWaitTimedOut,
        _NeverFits,
        _new_import_temp,
        _parse_factor,
        _read_bundle_body,
        _reject,
        _release_disk,
        _require_disk_room,
        _reserve_disk,
        _stalling_chunks,
        _stream_request_to_file,
        _tighter,
        _upload_admission,
        _uploads_in_flight,
        _UploadsBusy,
        _write_reserved,
    )
    from kiro_crew.dashboard.session_transfer_layers import (  # noqa: F401
        _events_jsonl_is_loadable,
        _forget_layer_b_join,
        _join_layer_b,
        _rewrite_layer_b_envelope,
        _snapshot_events_file,
        _unlink_layer_b_files,
        _write_layer_b_files,
    )
    from kiro_crew.dashboard.session_transfer_staging import (  # noqa: F401
        _CHUNK_BYTES,
        _MAX_TITLE_CHARS,
        _PROCESS_STARTED_AT,
        _SUPPORTED_BUNDLE_VERSIONS,
        LayerBEvents,
        _egress_tmp_dir,
        _import_tmp_dir,
        _rm_import_temps,
        _sweep_orphaned_staging,
        _swept_staging_dirs,
        _write_json_string_from_file,
        local_instance_label,
        write_bundle_file,
        write_bundle_json,
    )
