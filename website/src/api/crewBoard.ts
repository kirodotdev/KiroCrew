/**
 * Types for the Crew page's work-item board (`GET /api/crew-board`).
 *
 * These mirror the server's MASKED projection in
 * `kiro_crew/dashboard/handlers/work_ledger_board.py`, not the conductor's own
 * `work_ledger_read` shape. The difference is deliberate and is the point of the
 * endpoint: `worker_session_key` is absent from every row, and the text of a
 * `bind` event — which IS a session key — arrives blanked. So there is no field
 * here the page could use to address a worker session, which is why a row's
 * actions are keyed by `item_id` and resolved server-side.
 *
 * `alive`, `orphaned`, `stale`, `outstanding`, `terminal` and `acceptance_concrete`
 * are all DERIVED server-side rather than stored, so the page never recomputes
 * them: a second implementation of "is this stale" would drift from the store's.
 */

/** Item lifecycle state — the conductor's column. `open` plus three terminals. */
export type WorkItemState = 'open' | 'accepted' | 'rejected' | 'abandoned'

/** What the worker last said about itself — the worker's column. `null` before
 *  its first report, which is not the same as "fine". */
export type WorkReportStatus = 'progress' | 'done' | 'blocked' | 'question' | null

/** `accept_eval.py`'s own five values, carried through without translation. */
export type WorkVerdict = 'pass' | 'fail' | 'pending' | 'refused' | 'error' | null

/** Whether a session is behind the row. Joined server-side from the slot table
 *  because the key that resolves it is the one field the page may not receive. */
export type AliveState = 'running' | 'idle' | 'closed'

/** One line of an item's append-only event log. */
export interface WorkBoardEvent {
  id: string
  ts: string
  item_id: string
  kind: 'create' | 'bind' | 'report' | 'decision' | 'verdict' | 'close'
  /** Present on a `report` line: the status the worker reported. */
  status: WorkReportStatus
  /** Blanked by the server on a `bind` line, whose text is a session key. */
  text: string
}

/** One masked row: the conductor's item minus `worker_session_key`, plus joins. */
export interface WorkBoardItem {
  schema: number
  item_id: string
  title: string
  acceptance: Record<string, unknown>
  state: WorkItemState
  verdict: WorkVerdict
  /** The conductor's instruction to the worker. The one field a worker reads. */
  decision: string
  round: number
  fails: number
  status: WorkReportStatus
  /** The worker's own account of where the work is. Rendered as the row's
   *  last line, standing in for the session ledger's `next`, which this store
   *  has no equivalent of. */
  summary: string
  artifacts: Record<string, string>
  pr: number | null
  last_report_at: string | null
  created_at: string
  closed_at: string | null
  /** Nothing is left to read this item's reports. */
  orphaned: boolean
  /** The move is with the worker and it has gone quiet. */
  stale: boolean
  /** The bar is one `accept_eval.py` can actually evaluate. */
  acceptance_concrete: boolean
  /** A question with no NEWER answering decision. Lifts the row into the band. */
  outstanding: boolean
  terminal: boolean
  alive: AliveState
  events: WorkBoardEvent[]
}

/** The conductor's own record. No `worker_session_key` anywhere in it. */
export interface WorkBoardConductor {
  schema: number
  slot_key: string
  goal: string
  round: number
  depth: number
  parent_item: string | null
  created_at: string
}

export interface WorkBoardResponse {
  conductor: WorkBoardConductor
  conductor_alive: AliveState
  items: WorkBoardItem[]
  /**
   * Whether this gateway can take an orphaned item over. False on main: no
   * server-side primitive exists to delegate to, so the button renders disabled.
   *
   * The only capability field here, because it is the only one the page reads.
   * `stop_available`, `channels_available` and a reason code were sent too, so
   * Phase 5 and a future take-over could switch features on server-side -- but
   * nothing read them, and a field with no reader is a claim the payload makes and
   * the page ignores. Each belongs in the change that adds its reader.
   */
  take_over_available: boolean
}

/** The two affordances Phase 4 names for an orphaned item. */
export type CrewBoardAction = 'stop' | 'take_over'

/**
 * What an action answers. Deliberately narrow: the server allow-lists its reply
 * rather than passing the stop primitive's own body through, so nothing here can
 * name the session that was acted on.
 */
export interface CrewBoardActionResult {
  ok: boolean
  action: CrewBoardAction
  item_id: string
}

/** How often the browser re-reads the board. The RFC's figure: slow enough to
 *  cost nothing, fast enough that a decision request is noticed while the human is
 *  still at the desk. No websocket in this PR. */
export const CREW_BOARD_POLL_MS = 10_000

/** React Query key. A board belongs to ONE conductor, so the key carries it —
 *  two conductors open in two tabs must not share a cache entry. */
export function crewBoardQueryKey(conductor: string): readonly unknown[] {
  return ['crew-board', conductor]
}
