/** Derived board lanes: which live runtime state a session is in.
 *
 *  A tag column answers "what did someone file this session under". A lane
 *  answers "what is this session doing right now", which is the question a
 *  board of agent sessions actually has — and nothing writes a tag when an
 *  agent starts working or stops for an approval, so a tag board of workflow
 *  states never moves on its own.
 *
 *  Two properties make lanes safe to render a whole board from, and both are
 *  load-bearing rather than incidental:
 *
 *  - **Exhaustive.** `idle` is the fallback, so every session lands somewhere.
 *    A card that matched no lane would simply disappear from the board.
 *  - **Mutually exclusive.** The first matching rule wins, so a session appears
 *    in exactly one lane. A multi-tag session renders once per matching tag
 *    column; a lane card has one home, which is what makes the column counts
 *    add up to the session total.
 */

/** Lane ids. Declared with the wire types (the `state_key` field carries one)
 *  and mirrored by `_VALID_STATE_KEYS` in `dashboard/chat_tags.py`, which
 *  refuses a column naming anything outside this set. */
export type { SessionLaneKey } from '../../types'

import type { SessionLaneKey } from '../../types'

/** The slot fields a lane rule reads. A structural subset of `ChatSlot` so the
 *  rules stay testable without constructing a whole slot payload. */
export interface LaneSlotFields {
  pending_approval?: boolean
  needs_input?: boolean
  has_options?: boolean
  interrupted?: boolean
  running?: boolean
  orchestrating?: boolean
  subagents_running?: boolean
  queue_depth?: number
}

/** Signals that live outside the slot payload but change which lane is right.
 *
 *  `subagentAwaiting` is the one that matters: a parent session whose sub-agent
 *  is blocked on an approval is not running anything and would otherwise sort
 *  to `idle`, hiding an owed decision on the least-urgent lane. The count is
 *  tracked per slot in the dashboard store, never on the slot payload. */
export interface LaneExtras {
  subagentAwaiting?: number
  /** A dynamic workflow is active for this slot. Unlike an armed goal loop,
   *  this is current execution and supersedes an older interrupted turn. */
  workflowActive?: boolean
  /** An armed goal loop is work while healthy, but not while its last turn is
   *  interrupted — that stalled state remains Waiting until the next cycle. */
  goalLoopActive?: boolean
  /** Detailed child activity can lead or lag the slot snapshot during reconnect. */
  detailedSubagentsRunning?: boolean
}

/** Current execution that makes an older `interrupted` flag stale. Shared by
 *  the sidebar row and board lane so Resume and Working cannot disagree. An
 *  armed goal loop is deliberately excluded: it is future work, not execution. */
export function hasLiveSessionWork(slot: LaneSlotFields, extras: LaneExtras = {}): boolean {
  return !!(
    slot.running ||
    slot.orchestrating ||
    slot.subagents_running ||
    (slot.queue_depth ?? 0) > 0 ||
    extras.workflowActive ||
    extras.detailedSubagentsRunning
  )
}

export interface SessionLaneDef {
  key: SessionLaneKey
  /** i18n key for the column header. */
  labelKey: string
  /** Theme variable for the lane's accent dot and count. */
  color: string
}

/** Board order, most urgent first: the lanes that owe the user something come
 *  before the ones that do not, so the left edge of the board is the work that
 *  cannot advance without them. */
export const SESSION_LANES: readonly SessionLaneDef[] = [
  { key: 'needs_approval', labelKey: 'pages.chatSidebar.lane_needs_approval', color: 'var(--warn)' },
  { key: 'waiting', labelKey: 'pages.chatSidebar.lane_waiting', color: 'var(--info)' },
  { key: 'working', labelKey: 'pages.chatSidebar.lane_working', color: 'var(--accent)' },
  { key: 'idle', labelKey: 'pages.chatSidebar.lane_idle', color: 'var(--muted)' },
] as const

/** Which lane a session belongs to.
 *
 *  Precedence mirrors the row status chain in `ChatSidebar`: an owed decision
 *  outranks every "working" signal, because a blocking approval or question
 *  card leaves `running` true while nothing can actually advance. Ranking
 *  `working` first would file those sessions under a spinner and bury the very
 *  thing the board exists to surface.
 */
export function inferLane(slot: LaneSlotFields, extras: LaneExtras = {}): SessionLaneKey {
  // A tool gate the user owes, or a sub-agent's gate the parent owes.
  if (slot.pending_approval || (extras.subagentAwaiting ?? 0) > 0) return 'needs_approval'
  // Parked on a human answer. Deliberately NOT `waiting_for_input`, which is
  // true of every finished turn and would swallow the whole idle lane: only an
  // explicit unanswered question or options card outranks live work.
  if (slot.needs_input || slot.has_options) return 'waiting'
  if (hasLiveSessionWork(slot, extras)) return 'working'
  // An interrupted turn waits on the user only when no newer work supersedes
  // it. An armed goal loop does not supersede its own stalled turn.
  if (slot.interrupted) return 'waiting'
  if (extras.goalLoopActive) return 'working'
  return 'idle'
}
