/**
 * Shared types for the per-member projection store.
 *
 * The backend owns the authoritative vocabulary (see
 * src/kiro_crew/eventlog/types.py); these mirror the two WebSocket frame
 * shapes, the roster baseline block, and the four projected value shapes so
 * the frontend can read a projection without re-deriving it from raw events.
 */

/** Projection keys the store tracks. Kept as a union so callers name a real key. */
export type ProjectionKey = 'roster' | 'activity' | 'wake' | 'driving'

/** Baseline projections carried by each GET /api/members roster row. */
export interface ProjectionsBlock {
  asOfSeq: number
  values: { [key: string]: unknown }
}


/** The 'roster' projection: config-derived roster fields (minus live presence). */
export interface RosterView {
  name: string
  slug: string
  kiro_agent?: string
  workspace?: string
  memory_store?: string
  model?: string
  source?: string
  starred?: boolean
  avatar?: string
  /** Presentation label shown in place of `name` when non-empty. */
  display_name?: string
  slot_key?: string
  last_active_ts?: number
  last_message?: string
  /** Where each of this member's slots' CURRENT conversation begins, keyed by
   *  slot key, as epoch ms: the instant the `slot/reset` entry recorded (its own
   *  `ts`, which is when the conversation was discarded — NOT when its log line
   *  was appended, which is later). The fold keeps the GREATEST such instant per
   *  slot, so this value only ever moves forward. Absent for a slot that has
   *  never been reset. */
  conversation_starts?: { [slotKey: string]: { ts?: number } }
  /** How many boundaries the fold's own cap on `conversation_starts` has
   *  dropped for this member, oldest first. Absent means none, which is the
   *  ordinary case: a member drives one DM slot and the cap is 16. It is here
   *  because a dropped key reads exactly like a slot that was never reset — the
   *  pane would draw a discarded conversation as current — so the count is the
   *  only thing that tells "never reset" apart from "we had it and let it go".
   *  Advisory: it says how many, never which, and the moments are gone. */
  conversation_starts_evicted?: number
}

/** The 'activity' projection: recent participation records plus rolling counts. */
export interface ActivityView {
  recent: unknown[]
  today: number
  week: number
}

/** The 'wake' projection: the member's patrol (auto-nudge loop) state. */
export interface WakeView {
  patrol: 'armed' | 'stopped' | 'none'
  slot_key?: string
  stopped_reason?: string
  since?: number
}

