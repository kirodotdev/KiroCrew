/**
 * The line of names on Home's strip (`homeNames.ts`), as state: which names stand, and what a pinch on one does
 * (D65, D72). A name always does something, and says what:
 *
 * - `take`: it brings that Bot's board to his hand as a pinch on the board at its desk does: the same call, `take`
 *   of the boards' hook (`useBoards.ts`), with the id of the board that stands at the Bot. A board that is about to
 *   be made is taken as it comes. A Bot that failed is first tapped as a tap on it at its desk taps it
 *   (`Workspace.tsx`, `tapBot`): its last words come up and are taken as they come.
 * - `fetch`: that failed Bot's words are on their way (`WORDS_WAIT_MS`); the name is dimmed and takes no pinch.
 *   Words that come later stay at the Bot, and the name stays.
 * - `page`: no board can come for its asks (an ask with no request to answer, a Bot that is not drawn): the pinch
 *   opens that Bot's page ahead, as a line of Home does (`useHomeLines.ts`, `openLine`). Never when a board can come.
 * - `none`: that Bot's board is in his hand unstamped, or on the floor: one ask is never in two places.
 *
 * An ask that a board answered is not counted: at his stamp "Atlas 2" reads "Atlas 1", and comes back if the stamp's
 * send fails (`useBoards.ts`, `send`). A pinch on that name while the STAMPED board is still in his hand files it as
 * a throw of it does (`release`), and the Bot's next board is taken as it comes: one pinch from one approval to the
 * next. An unstamped board is never let go by a name. In a session only: the flat views keep Home as it was.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject } from 'react'
import type { XrBot } from '../model'
import { boardAskOf, heldFull } from './board'
import { LINE_WAIT_MS } from './home'
import { HOME_NAMES, fitLine, homeNames, type HomeName } from './homeNames'
import type { useBoards } from './useBoards'

export type NameDoes = 'take' | 'fetch' | 'page' | 'none'

/** A failed Bot's words are read from the gateway after the tap: taken into his hand when they come within this long (ms). */
export const WORDS_WAIT_MS = 6000
/** His dot counts as off the line once it has been on no name for this long (ms): from one name to the next it is not. */
export const OFF_LINE_MS = 250

interface Inputs {
  crew: readonly XrBot[]
  /** The Bots' states can be known: a name that may be wrong is not shown, as a number is not. */
  known: boolean
  inSession: boolean
  boards: Pick<ReturnType<typeof useBoards>, 'views' | 'take' | 'release' | 'answered'>
  /** The Bots whose bodies are drawn: only such a Bot holds up a board (`board.ts`, `boardsNow`). */
  drawnBots: ReadonlySet<string>
  /** A tap on a Bot at its desk (`Workspace.tsx`, `tapBot`). */
  tap: (slug: string) => void
  /** That Bot's own page, ahead (`Workspace.tsx`, `openBot`). */
  openBot: (slug: string) => void
  /** When a pointer last went down anywhere, or a pinch began: a name's wait for its board ends there. */
  anyDownAt: MutableRefObject<number>
}

const NONE: { names: HomeName[]; more: number } = { names: [], more: 0 }
/** A release that reads as a throw (`board.ts`, `releaseKind`): the hand went far and was fast. */
const FILED = { handM: 1, handMps: 10, onBot: false } as const

type View = ReturnType<typeof useBoards>['views'][number]
/** A stamped board in his hand: its ask is answered, and it only waits to be filed. */
const isFiled = (b: View) => b.where === 'held' && b.stamped

export function useHomeNames({ crew, known, inSession, boards, drawnBots, tap, openBot, anyDownAt }: Inputs) {
  const { views, take, release, answered } = boards
  /** The crew with the asks that a board answered left out. The boards stand for what `answered` reads. */
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const open = useMemo(() => { const done = answered(); return crew.map(b => (b.needs.some(n => done.has(n.id)) ? { ...b, needs: b.needs.filter(n => !done.has(n.id)) } : b)) }, [crew, views, answered])
  /** The askers in the order they came to the line: a name keeps its place while it asks (D75). A reload starts anew. */
  const came = useRef<string[]>([])
  const fresh = useMemo(() => {
    if (!inSession || !known) { came.current = []; return NONE }
    const all = homeNames(open, Infinity, came.current).names
    came.current = all.filter(n => !n.failed).map(n => n.slug)
    return fitLine(all)
  }, [inSession, known, open])
  const freshNow = useRef(fresh)
  freshNow.current = fresh
  // While his dot is on the line, a name that leaves keeps its place, empty: the names after it do not move under
  // his aim. The gap closes when the dot is off the line.
  const [names, setNames] = useState(fresh)
  const overNow = useRef(false)
  const offTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    setNames(was => {
      if (!overNow.current || !was.names.length || !fresh.names.length) return fresh
      const kept = was.names.map(p => fresh.names.find(n => n.slug === p.slug) ?? { ...p, gone: true })
      // Kept and new together never pass `most` (B10): the ones that do not fit are counted in "+n".
      const shown = [...kept, ...fresh.names.filter(n => !was.names.some(p => p.slug === n.slug))].slice(0, HOME_NAMES.most)
      return { names: shown, more: fresh.names.length + fresh.more - shown.filter(n => !n.gone).length }
    })
  }, [fresh])
  /** Home says whether his dot is on a name's place. Between two names it is off for a moment: that closes no gap. */
  const over = useCallback((on: boolean) => {
    if (offTimer.current) { clearTimeout(offTimer.current); offTimer.current = null }
    if (on) { overNow.current = true; return }
    offTimer.current = setTimeout(() => { offTimer.current = null; overNow.current = false; setNames(was => (was.names.some(n => n.gone) ? freshNow.current : was)) }, OFF_LINE_MS)
  }, [])
  useEffect(() => () => { if (offTimer.current) clearTimeout(offTimer.current) }, [])
  const viewsNow = useRef(views)
  viewsNow.current = views
  /** The failed Bot whose words are on their way. */
  const [fetching, setFetching] = useState<string | null>(null)
  /** The name that was pinched before its Bot's board stood at it: the board is taken as it comes, within its wait. */
  const waits = useRef<{ slug: string; at: number; timer: ReturnType<typeof setTimeout>; tap?: boolean } | null>(null)
  const endWait = useCallback(() => { const w = waits.current; waits.current = null; if (w) clearTimeout(w.timer); setFetching(null) }, [])
  useEffect(() => endWait, [endWait])
  useEffect(() => { endWait() }, [inSession, endWait])
  /** What a pinch on each name does now. */
  // He writes on both boards in his hand: a take does nothing now, so no name takes (D79 2).
  const full = heldFull(views)
  const doing = useMemo(() => new Map(names.names.map(n => {
    const own = views.filter(b => b.bot === n.slug)
    const does: NameDoes = n.gone ? 'none'
      : own.some(b => b.where === 'bot' && b.kind !== 'speak') ? 'take'
      // A board he still writes on is never filed by a name (D76 2); nor is one that is not stamped.
      : own.some(b => !isFiled(b) || b.writing) ? 'none'
        : fetching === n.slug ? 'fetch'
          : n.failed ? 'take'
            : drawnBots.has(n.slug) && boardAskOf(open.find(b => b.slug === n.slug) ?? { needs: [] }) ? 'take' : 'page'
    return [n.slug, full && does === 'take' ? 'none' : does] as const
  })), [names, views, fetching, drawnBots, open, full])
  const doingNow = useRef(doing)
  doingNow.current = doing
  const does = useCallback((slug: string): NameDoes => doing.get(slug) ?? 'none', [doing])
  /** The board that stands at the Bot now, to be taken: not the sheet he speaks on. */
  const atBot = (slug: string) => viewsNow.current.find(b => b.bot === slug && b.where === 'bot' && b.kind !== 'speak')
  const press = useCallback((slug: string) => {
    const act = doingNow.current.get(slug) ?? 'none'
    if (act === 'none' || act === 'fetch') return
    endWait()
    if (act === 'page') { openBot(slug); return }
    const board = atBot(slug)
    if (board) { take(board.needId); return }
    // Its stamped board is in his hand: it is filed as a throw files it (his finished note goes to the Bot with it,
    // `useBoards.ts`, `release`), and the Bot's next board comes.
    const filed = viewsNow.current.filter(b => b.bot === slug && isFiled(b))
    for (const b of filed) release(b.needId, FILED)
    // Nothing stands at it: a Bot that failed is tapped for its last words; an ask's board is about to be made.
    // The desk's tap asks for no words while a board of that Bot is in the room, so a tap after a filing waits until
    // the filed board has left the boards (W2).
    const failed = names.names.find(n => n.slug === slug)?.failed ?? false
    if (failed) { if (!filed.length) tap(slug); setFetching(slug) }
    const at = performance.now()
    waits.current = { slug, at, timer: setTimeout(endWait, failed ? WORDS_WAIT_MS : LINE_WAIT_MS), tap: failed && filed.length > 0 }
  }, [endWait, names, take, release, tap, openBot])
  useEffect(() => {
    const w = waits.current
    if (!w) return
    // He pressed something else since: no board flies into his hand after he moved on.
    if (anyDownAt.current > w.at) { endWait(); return }
    if (w.tap && !views.some(b => b.bot === w.slug)) { w.tap = false; tap(w.slug) }
    const board = atBot(w.slug)
    if (board) { endWait(); take(board.needId) }
  }, [anyDownAt, views, endWait, take, tap])
  return { names, press, does, over }
}
