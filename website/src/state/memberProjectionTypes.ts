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
  /**
   * Per-key generation for CONTRIBUTED rows, which orders ahead of the seq. The
   * backend sends it only when an app has published to this member, and never for
   * a built-in key, so it is optional and an absent entry reads as 0 — which
   * collapses that row's ordering to plain higher-seq-wins.
   */
  stateVersions?: { [key: string]: number }
  /**
   * Per-key SEQ for CONTRIBUTED rows. A contributed row's seq is the contributor's
   * own fold position, which trails this response's `asOfSeq`, so seeding the row at
   * `asOfSeq` would make the ordering gate drop that contributor's next live push and
   * freeze the card at its baseline. Absent for a built-in key, whose seq IS
   * `asOfSeq`, and absent entirely when no app has published to this member.
   */
  seqs?: { [key: string]: number }
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

