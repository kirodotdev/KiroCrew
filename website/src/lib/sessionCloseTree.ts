/**
 * Closing a session tree — what the close would take down, as a pure function.
 *
 * The session tree's ✕ closes a card and the sessions nested under it. A lead with
 * three workers, each with sub-workers, is one card over seven sessions, and the
 * press is unrecoverable: an in-flight turn is cancelled and its partial work is
 * discarded. So before anything closes, the UI has to be able to say what the press
 * reaches and how much of it is still working.
 *
 * This module answers exactly that and does nothing else: no React, no store, no
 * I/O, no `deleteSlot`. The dialog copy and the close loop both read the same plan,
 * so the list a user consents to cannot differ from the sessions that are closed.
 *
 * KEY SPACE. Plans are built and returned in RAW slot-key space — the space
 * `deleteSlot` takes — not in the sidebar's origin-qualified row identities. A
 * federated peer row has no local slot to close, so it is not plannable from here
 * and callers pass their local rows.
 */

import { buildLineage, descendantsOf, type LineageRow } from './sessionLineage'

/** One session in the plan, as the dialog renders it. */
export interface CloseTreeSession {
  key: string
  /** The row's own title, or its key when it has none — never empty. */
  title: string
  running: boolean
  /** Running only through background work (live subagents) while its own turn is
   *  idle; the notice labels it so, because the row's own status line looks idle. */
  background: boolean
  /** The server's `history_key` Undo reopens this session under, or `''` when the
   *  payload carries none (then Undo is not offered for it). Kept apart from
   *  `key`, which is the slot `deleteSlot` closes. */
  historyKey: string
}

/**
 * One sub-level of the tree. `depth` is RELATIVE to the session being closed: 0 is
 * that session itself, 1 its direct children, and so on. Relative, because the
 * dialog describes one press on one card, and the card's own absolute depth in the
 * sidebar is not a fact about what this press does.
 */
export interface CloseTreeLevel {
  depth: number
  sessions: CloseTreeSession[]
  runningCount: number
}

export interface CloseTreePlan {
  /**
   * Every key this close takes down, DEEPEST FIRST with the pressed session last.
   *
   * The order is load-bearing, not cosmetic: a parent archived while a child still
   * cites it as a live creator leaves the child re-rendered as an orphan mid-close,
   * and the backend's own close path reads the parent edge. Closing upward means
   * each session is closed only once nothing under it is still open.
   */
  order: string[]
  /** Level 0 (the session itself) first. Empty only for an unknown key. */
  levels: CloseTreeLevel[]
  /** `order.length` — the session plus its descendants. */
  total: number
  /** Sessions under the pressed one. `total - 1`, or 0 for an unknown key. */
  descendantCount: number
  /** How many DESCENDANTS are still running, the pressed session not counted. */
  runningDescendants: number
  /** Whether the pressed session itself is still running. It gates the close
   *  exactly as a running descendant does: closing would cancel its turn. */
  leadRunning: boolean
  /**
   * How many of the sessions in `order` are still running, the pressed one
   * included: `runningDescendants` plus one when `leadRunning`.
   */
  runningTotal: number
  /**
   * True when the pressed session or any descendant is still running. On this
   * plan, the ✕ is REFUSED: nothing closes, and the notice lists which sessions
   * are still working.
   * False means the whole subtree closes with no prompt.
   */
  blocked: boolean
  /**
   * Each planned session's parent INSIDE this plan (the pressed session has none).
   * The close loop reads it to keep every ancestor of a refused close open, so a
   * refusal never leaves a worker standing alone without its lead.
   */
  parentOf: ReadonlyMap<string, string>
}

const EMPTY_PLAN: CloseTreePlan = {
  order: [], levels: [], total: 0, descendantCount: 0,
  runningDescendants: 0, leadRunning: false, runningTotal: 0, blocked: false, parentOf: new Map(),
}

/**
 * Whether a sidebar row counts as running for a tree close: a turn in flight,
 * live subagents the stream has replayed, OR the slot's own `subagents_running`
 * snapshot. The snapshot matters on a reload or reconnect, where it arrives
 * before the subagent replay fills the counts; without it a descendant with
 * live subagents reads idle and the close would cancel them.
 */
export function sidebarRowRunning(
  row: { key: string; subagents_running?: boolean },
  runningKeys: ReadonlySet<string>,
  subagentCounts: Readonly<Record<string, number>>,
): boolean {
  return runningKeys.has(row.key) || sidebarRowBackgroundOnly(row, runningKeys, subagentCounts)
}

/** Running ONLY through background work: no turn in flight, but live subagents
 *  (replayed count, or the slot's own `subagents_running` snapshot). */
export function sidebarRowBackgroundOnly(
  row: { key: string; subagents_running?: boolean },
  runningKeys: ReadonlySet<string>,
  subagentCounts: Readonly<Record<string, number>>,
): boolean {
  return !runningKeys.has(row.key) && ((subagentCounts[row.key] ?? 0) > 0 || row.subagents_running === true)
}

/**
 * A row this module can plan over: a lineage edge, a title to show, and the
 * server's `history_key` — the transcript stem the Older sessions list gives the
 * same session, which is the only key Undo may reopen it under. The client never
 * derives it: whether an unbound slot's transcript is the dashboard's or a
 * channel's is provenance the server holds and the slot name does not carry.
 */
export type CloseTreeRow = LineageRow & { title?: string; history_key?: string }

/**
 * What closing *key* takes down, given the rows currently on screen.
 *
 * `isRunning` is the CALLER's predicate on purpose. The sidebar's notion of running
 * is wider than the payload's `slot.running` — a live workflow run or an armed goal
 * loop counts — and the dialog has to mark exactly the rows the lane draws as
 * running, or it would contradict the tree it is covering.
 *
 * An unknown key yields the empty plan rather than a throw: a stale press (the row
 * went away while the pointer was over it) must close nothing, not crash the sidebar.
 */
export function planCloseTree<R extends CloseTreeRow>(
  rows: readonly R[],
  key: string,
  isRunning: (row: R) => boolean,
  isBackgroundOnly: (row: R) => boolean = () => false,
): CloseTreePlan {
  const byKey = new Map<string, R>()
  for (const row of rows) if (row.key) byKey.set(row.key, row)
  const root = byKey.get(key)
  if (!root) return EMPTY_PLAN

  // The same lineage the lane nests by, so the plan cannot place a session
  // somewhere the user does not see it. `buildLineage` already refuses cycles and
  // parents absent from the payload, so the worst a strange payload yields is a
  // flatter plan.
  const { children, depth, parentOf: lineageParent } = buildLineage(rows)
  const rootDepth = depth.get(key) ?? 0

  const session = (row: R): CloseTreeSession => ({
    key: row.key,
    title: row.title?.trim() || row.key,
    running: isRunning(row),
    background: isRunning(row) && isBackgroundOnly(row),
    historyKey: typeof row.history_key === 'string' ? row.history_key : '',
  })

  const lead = session(root)
  const byLevel = new Map<number, CloseTreeSession[]>([[0, [lead]]])
  let runningDescendants = 0
  // Walked over `rows`, not over `descendantsOf`'s own return: that one is a
  // stack-order DFS, so siblings come back reversed. Taking the input order means
  // the dialog lists siblings exactly as the lane above it does.
  const inSubtree = new Set(descendantsOf(key, children))
  for (const row of rows) {
    const childKey = row.key
    if (!inSubtree.has(childKey)) continue
    const entry = session(row)
    if (entry.running) runningDescendants += 1
    // Relative to the pressed card. Clamped at 1: a payload that somehow placed a
    // descendant no deeper than its root still belongs on a sub-level, never back
    // on level 0 beside the session being closed.
    const level = Math.max(1, (depth.get(childKey) ?? rootDepth + 1) - rootDepth)
    const bucket = byLevel.get(level)
    if (bucket) bucket.push(entry)
    else byLevel.set(level, [entry])
  }

  const levels: CloseTreeLevel[] = [...byLevel.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([levelDepth, sessions]) => ({
      depth: levelDepth,
      sessions,
      runningCount: sessions.filter(s => s.running).length,
    }))

  // Deepest level first, the pressed session last. Within a level the lane's own
  // order is kept, so the dialog lists siblings the way the sidebar does.
  const order = [...levels].reverse().flatMap(level => level.sessions.map(s => s.key))
  const parentOf = new Map<string, string>()
  for (const childKey of inSubtree) {
    const parent = lineageParent.get(childKey)
    if (parent != null) parentOf.set(childKey, parent)
  }
  return {
    parentOf,
    order,
    levels,
    total: order.length,
    descendantCount: order.length - 1,
    runningDescendants,
    leadRunning: lead.running,
    runningTotal: levels.reduce((n, level) => n + level.runningCount, 0),
    blocked: lead.running || runningDescendants > 0,
  }
}
