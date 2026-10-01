/** View types the sidebar's owners share. `Slot` is the sidebar's narrowed view of a
 *  chat slot; the wire type is `ChatSlot` in ../../types. */
import type { SessionLink, SourceProviderId } from '../../types'

export interface Slot {
  key: string
  title?: string
  running: boolean
  /**
   * The session that OPENED this one through `session_create`, or null when nobody
   * did. Comes straight off the slots broadcast (`_attach_slot_parents`).
   *
   * `slot` is this session's own citation, read from its crew log, and survives
   * everything. `key` is the creator's row key IN THIS PAYLOAD -- so a bare slot key
   * here, matching `Slot.key` -- and is null when the creator is not running or the
   * records formed a cycle. A row can therefore cite a creator it cannot nest under,
   * which is the orphan the conductor lane marks with a muted prefix.
   *
   * `hub_key` appears only on a PEER row (see `useInstanceSessions`): the hub stamps
   * it when the creator is a peer slot this hub drives, and it names the LOCAL row
   * that drives that creator. It is the one half of a citation that crosses origins
   * on purpose -- resolved against local rows, so a worker a remote-executed lead
   * opened on the peer nests under the lead's local row.
   */
  parent?: { slot?: string; key?: string | null; hub_key?: string } | null
  /** Present and true while the gateway's lineage projection is still seeding for the
   *  current store, which makes THIS frame's `parent` provisional rather than final.
   *  Absent on an ordinary frame, and absent when there is nothing to wait for (the
   *  crew log is off), so it never asks a client to come back for an answer that will
   *  not change. */
  lineage_pending?: boolean
  /** Peer OWNERSHIP, present ONLY on a row sourced from a connected remote
   *  instance's live slot list (see `useInstanceSessions`). Absent on every local
   *  slot, so a consumer tests presence to decide whether the local-only
   *  affordances — close, duplicate, rename, pin, drag-reorder, folder move —
   *  apply at all.
   *
   *  DELIBERATELY NOT `instance_id`, which is declared below and means something
   *  else: that field names the peer a LOCAL slot DISPATCHES its turns to
   *  (`executor: 'remote'`). The row still lives here, is renameable, pinnable
   *  and activatable, and its history is local. Spelling both meanings with one
   *  field would make every guard in the sidebar misread a remote-executed local
   *  session as a session belonging to another machine — stripping its rename,
   *  drag, pin, folder and active-highlight and sending a click to the peer's
   *  dashboard instead of to the session the user asked for. It would also let
   *  our stamp overwrite a peer's own execution binding, since a peer's slot can
   *  itself be bound to a third machine. */
  peer_id?: string
  /** The identity this row renders under, resolved by the SERVER.
   *
   *  A purely local session is its own `key`. A remote-bound one — minted on a crew
   *  or adopted from a peer row — is `<instance_id>:<peer_key>`, the identity the
   *  peer row already carried. Preserving it across the bind is what makes the row
   *  the user clicked BECOME the session instead of a sibling appearing next to it.
   *
   *  Absent on a peer row and on an older payload; `sessionRowIdentity` falls back
   *  to `peer_id` + `key` for those. Never parse it to recover the local slot key —
   *  read `key`. */
  row_identity?: string
  peer_name?: string
  unread?: boolean
  // `pending_approval` rides on every ChatSlot payload; the sidebar reads it to
  // suppress the "your turn" dot and show the yellow "Needs approval" subtitle.
  pending_approval?: boolean
  // An unanswered question card the turn is parked on. Its own subtitle, and it
  // suppresses the "your turn" dot for the same reason an approval does.
  needs_input?: boolean
  // The NEWEST assistant reply ends with an `[OPTIONS:]` ask (payload
  // `has_options`). Read by the loop-waiting subtitle: an armed loop whose
  // newest reply is an explicit ask is holding for the user, not working.
  // Newest-reply-only by construction — any later turn that talks over the
  // marker clears it, so a superseded ask can never be resurrected (#10615).
  has_options?: boolean
  // The transcript shows the last turn ending without a reply (trailing error
  // row or unanswered user row) — the state behind the composer's Resume
  // button. Always false while a turn runs. Read by the goal-loop subtitle so a
  // stalled loop stops pulsing as if it were working.
  interrupted?: boolean
  // The slot snapshot can report live child work before the detailed activity
  // map hydrates after reconnect. Never present that gap as an idle interruption.
  subagents_running?: boolean
  // Autopilot orchestration and queued turns are also server-rejected Resume
  // states, even when the slot's own turn is currently idle.
  orchestrating?: boolean
  queue_depth?: number
  mode?: string
  /** Which page renders this session; the backend mirrors `mode` into it. The chat
   *  page admits only the surfaces `isChatPageSurface` names, so a row carrying any
   *  other value reached the sidebar through the conductor lane's creator anchors
   *  and opens elsewhere. */
  surface?: string
  agent?: string
  // The agent that will actually answer, when it is NOT `agent`. The backend
  // stores `agent` verbatim — it is the user's intent, and rewriting it on disk
  // was destructive — and reports the divergence here instead. "" / absent means
  // NOTHING TO REPORT, which covers both "the request is honored" and "resolution
  // is not settled yet" (a cold snapshot during boot). So it must be read as a
  // positive claim only: a falsy value never means "mismatch".
  effective_agent?: string
  model?: string  // '' / absent = provider-default ("auto")
  // Message count from the slot payload. Already carried by every ChatSlot
  // (redux seeds it in addSlotOptimistic and SessionGridView renders it); it was
  // simply never declared on this local view of the type.
  messages?: number
  workspace?: string
  /** Remote-execution binding — see `ChatSlot` in ../../types. Declared on this
   *  narrowed view too: the row reads `executor` to decide whether to render the
   *  crew chip, and a field absent from this interface is invisible to it no
   *  matter what the backend sends. */
  executor?: 'local' | 'remote'
  instance_id?: string
  created?: string
  last_ts?: string
  // Settled activity instant: the last prompt or turn completion, NOT every
  // streamed row. What the list is ordered, segmented and labelled by — see
  // `slotActivityTs`.
  last_turn_ts?: string
  last_message?: string
  slack_linked?: boolean
  links?: SessionLink[]
  color_index?: number | null
  color_hex?: string | null
  memory_mode?: 'persistent' | 'incognito' | 'temporary'
  folder_id?: string
  pinned?: boolean
  tags?: string[]
  forked_from?: string | null
  source_links?: Array<{
    provider: SourceProviderId
    number: number
    url: string
    // What the chip is called, decided by the serializer (`source_ref_label`):
    // `#123`, `!123`, `PROJ-123`. Not translated — a provider's identifier for
    // one of its own objects reads the same in every locale.
    //
    // OPTIONAL on the wire for the same reason `kind` is: a bundle newer than
    // the gateway it talks to must keep rendering. See `chipLabel`.
    label?: string
    ci?: 'running' | 'passed' | 'failed' | null
    state?: 'open' | 'draft' | 'merged' | 'closed'
    // Owner-gated chips spread the whole cached chip-status entry, which also
    // carries the settled merge pair. Present only once the provider settled it.
    mergeable?: string
    mergeStateStatus?: string
    // What the link points at. OPTIONAL on the wire — absent means 'change', so
    // older payloads and existing fixtures keep rendering as PR/MR chips.
    kind?: 'change' | 'issue'
    // The server-authoritative identity string this chip is keyed by, sent so
    // the client can pass it as ``expect`` to the unlink DELETE endpoint.
    // OPTIONAL on the wire so a bundle newer than its gateway still renders
    // chips (they just cannot be unlinked until the gateway sends it — the
    // affordance hides when it is absent).
    identity?: string
  }>
  source_links_total?: number
}

export type SourceLinkState = NonNullable<NonNullable<Slot['source_links']>[number]['state']>
/** One sidebar chip's payload, as the slot serializer sends it. */
export type SidebarSourceLink = NonNullable<Slot['source_links']>[number]

export interface HistoryItem {
  key: string
  title?: string
  created?: string
  modified?: number  // unix epoch seconds; backend's mtime — used for segmenting + display
  agent?: string  // persisted in JSONL metadata (set on session create + agent switch)
  memory_mode?: 'persistent' | 'incognito' | 'temporary'
  folder_id?: string  // folder the session was filed in; used to group search results
}

export interface AgentInfo {
  name: string
  source: string
  /** Default session color (#rrggbb) for this agent, applied at render time to
   *  sessions created by it that carry no explicit per-session color. */
  session_color?: string
}

export type SessionFilterKey = 'unread' | 'running' | 'pinned' | 'recent'

/**
 * Which session lane the list renders. One persisted preference, three values.
 *
 * `tree` is the folder hierarchy. `flat` explodes every chat out of its folder into
 * one recency-sorted lane. `conductor` nests each session under the session that
 * OPENED it (`session_create`), which is a different axis from folders entirely: a
 * conductor and the workers it spawned are one unit of work wherever their folders
 * put them.
 *
 * An enum rather than two booleans because the lanes are mutually exclusive, and two
 * independent flags would have a fourth state ("flat AND conductor") that means
 * nothing and that every render site would have to decide about.
 */
export type SidebarLane = 'tree' | 'flat' | 'conductor'

/** One filter dimension that can hide a reveal target: whether it hides THIS
 *  row, and how to drop it. `clear` receives the row because the folder filter
 *  un-hides that row's own ancestor chain rather than clearing globally. */
export interface RevealBlockingFilter {
  hides: (slot: Slot) => boolean
  clear: (slot: Slot) => void
}
/** One sidebar filter dimension, declared exactly once (in the component's
 *  `filterDimensions` memo) and consumed by the three sites that must agree on
 *  which filters exist: `filteredSlots` (which rows render at all),
 *  `listNarrowed` (is anything filtering right now), and
 *  `revealBlockingFilters` (does THIS row fail an active filter). Every field
 *  is required, so adding a dimension forces a decision for each consumer —
 *  `null` records "deliberately not consulted here", never an omission. */
export interface FilterDimension {
  /** Row predicate applied by `filteredSlots`. `null` = this dimension does
   *  not filter the flat slot list (the folder filter drops whole folder
   *  blocks/lanes at the render sites instead of filtering rows). */
  filtersRow: ((slot: Slot) => boolean) | null
  /** Is this dimension narrowing the list right now? Consulted by
   *  `listNarrowed`. `null` = deliberately excluded from that question (the
   *  folder filter: counting it would strand every folder as an empty
   *  "New chat in <name>" shell while one is hidden). */
  narrows: (() => boolean) | null
  /** Does this dimension hide THIS row from a reveal? `excluded` reports list
   *  membership, for dimensions (search, status) that rank against backend
   *  state a single row cannot answer for alone. Non-nullable on purpose,
   *  together with `clear`: every dimension can hide a reveal target today.
   *  If one ever genuinely cannot, make the PAIR nullable in one move —
   *  never stub `hides: () => false` beside a real `clear` (or a real
   *  `hides` beside a no-op `clear`, which is silent reveal breakage). */
  hides: (slot: Slot, excluded: (slot: Slot) => boolean) => boolean
  /** Drop this dimension so the reveal target renders. Receives the row
   *  because the folder filter un-hides that row's own ancestor chain rather
   *  than clearing globally. */
  clear: (slot: Slot) => void
}
