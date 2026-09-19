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

/**
 * A whole projected value pushed over the WebSocket.
 * Client rule: higher seq wins; replays and stale frames drop.
 */
export interface MemberProjectionFrame {
  type: 'member_projection'
  slug: string
  key: string
  value: unknown
  seq: number
}

/**
 * Sent once per connection, before any member_projection frame.
 * Client rule: drop held rows whose seq > lastSeq for that slug (a torn tail
 * the server truncated after a restart).
 */
export interface MembersSubscribedFrame {
  type: 'members_subscribed'
  lastSeqs: { [slug: string]: number }
}

/** Baseline projections carried by each GET /api/members roster row. */
export interface ProjectionsBlock {
  asOfSeq: number
  values: { [key: string]: unknown }
  /**
   * Per-key seq, present only for CONTRIBUTED rows (contribution protocol §5).
   * A contributed row's seq is the contributor's own fold position, not this
   * response's asOfSeq, so the store seeds it at its own seq -- otherwise
   * higher-seq-wins would drop the contributor's next live push.
   */
  seqs?: { [key: string]: number }
  /** Per-key rendering, for contributed rows whose app published one (§7). */
  schemas?: { [key: string]: ProjectionSchema }
}

/**
 * How a contributor asks a dashboard to render one of its views (§7). Declared
 * once per key, never code: no contributor JavaScript runs in the browser.
 *
 * `kind` picks the body shape. `path` is a list of dotted selectors into the
 * value, read differently per kind: the first selector picks the array for
 * `list` and `table`, and the whole list names the fields to show for
 * `keyvalue`, `badge` and `text`. An absent or unusable schema falls back to a
 * compact key-value dump of the value.
 */
export interface ProjectionSchema {
  kind: 'badge' | 'text' | 'list' | 'table' | 'keyvalue'
  title?: string
  path?: string[]
}

/** One contributed `<app>/<key>` view held for a member. */
export interface ContributedView {
  key: string
  value: unknown
  seq: number
  schema?: ProjectionSchema
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

/** The 'driving' projection: slot keys the member currently drives. */
export interface DrivingView {
  open: string[]
}
