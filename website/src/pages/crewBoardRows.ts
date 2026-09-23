/**
 * Pure helpers for the Crew work-item board's three bands.
 *
 * Extracted from the page for the same reason as the sibling `staleCollapse` and
 * `recentWindow` modules: the ordering rules are the part worth pinning, and
 * pinning them through a render costs a DOM and proves less.
 *
 * The board has three bands, top to bottom:
 *
 *   1. "Needs a decision" — items waiting on the conductor's own answer. Lifted to
 *      the top because a board whose whole purpose is to surface a blocked worker
 *      fails if the human has to scroll to find one.
 *   2. Working — everything still open.
 *   3. Finished — terminal items, collapsed behind an expander.
 *
 * `outstanding` and `terminal` both arrive from the server, already derived. This
 * module only decides which band wins when both are set, and that is the one
 * judgement it makes.
 */

import type { WorkBoardItem } from '../api/crewBoard'

export interface BoardBands {
  /** Waiting on the conductor. Rendered first, whatever their age. */
  ruling: WorkBoardItem[]
  /** Open and not waiting on a decision. */
  working: WorkBoardItem[]
  /** Terminal. Collapsed behind an expander. */
  finished: WorkBoardItem[]
}

/**
 * Split the server's item list into the three bands, preserving input order
 * within each.
 *
 * TERMINAL WINS over outstanding, and the ordering matters. An item can carry
 * `status: "question"` and still be closed — the conductor answered it by
 * closing it, and `accepted`/`rejected`/`abandoned` is that answer. Lifting such
 * a row into "Needs a decision" would ask the human to rule on work that is already
 * finished, which is the one thing a decision band must never do: every row in it
 * has to be actionable or the band stops being read.
 *
 * The input is not mutated — the caller's array is React Query's cached data.
 */
export function partitionBoardRows(items: readonly WorkBoardItem[]): BoardBands {
  const ruling: WorkBoardItem[] = []
  const working: WorkBoardItem[] = []
  const finished: WorkBoardItem[] = []
  for (const item of items) {
    if (item.terminal) finished.push(item)
    else if (item.outstanding) ruling.push(item)
    else working.push(item)
  }
  return { ruling, working, finished }
}

/**
 * The row's trailing kind label — the right-aligned word naming what this row is.
 *
 * Deliberately NOT the alive state, which has its own chip: this answers "what
 * kind of row am I looking at", so a scan down the right edge reads as a list of
 * situations rather than a list of session states. `orphaned` outranks `stale`
 * because it is the worse fact — a stale item still has someone to report to, an
 * orphaned one does not.
 *
 * Returns `null` for a terminal row, which means "render `item.state`". A closed
 * item's kind IS its state — `accepted` / `rejected` / `abandoned` — and that
 * word is the store's own vocabulary arriving as data, so translating it would
 * put a different word on the page from the one every tool and log reports.
 */
export function rowKindLabelKey(item: WorkBoardItem): string | null {
  if (item.orphaned) return 'pages.crewBoard.kind_orphaned'
  if (item.outstanding) return 'pages.crewBoard.kind_ruling'
  if (item.stale) return 'pages.crewBoard.kind_stale'
  if (item.terminal) return null
  return 'pages.crewBoard.kind_working'
}

/**
 * The artifacts worth a link, in a stable order.
 *
 * `pr` is a number on its own field rather than an artifact string, so it is
 * pulled out first and the map's own `pr` key — if a worker also wrote one — is
 * skipped rather than rendered twice saying possibly different things.
 */
export function artifactEntries(item: WorkBoardItem): Array<[string, string]> {
  const out: Array<[string, string]> = []
  for (const [key, value] of Object.entries(item.artifacts ?? {})) {
    if (!value) continue
    if (item.pr !== null && key === 'pr') continue
    out.push([key, value])
  }
  // Byte order, not `localeCompare`: these are artifact KEYS the store defines
  // (`branch`, `commit`, `pr`, a path), never translated prose, so what is wanted
  // is one stable order every reader sees. A locale collation would make the row
  // order depend on the viewer's browser and would reorder the same board between
  // two people looking at it together.
  out.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
  return out
}
