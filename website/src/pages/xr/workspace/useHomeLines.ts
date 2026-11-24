/**
 * A press on a line of what waits on the Home surface (`home.ts`, `lineDoes`), and the wait of a line pressed before
 * its board came. Moved out of `Workspace.tsx` whole; the room passes in what it reads.
 */
import { useCallback, useEffect, useMemo, useRef, type MutableRefObject } from 'react'
import type { XrBot } from '../model'
import { boardAskOf } from './board'
import { HOME_ASKS_PAGE, LINE_WAIT_MS, behindTakenBoard, boardComes, lineDoes, type HomeLine } from './home'
import type { useBoards } from './useBoards'

interface Inputs {
  lines: readonly HomeLine[]
  boards: ReturnType<typeof useBoards>
  crew: readonly XrBot[]
  crewNow: MutableRefObject<readonly XrBot[]>
  drawnBots: ReadonlySet<string>
  inSession: boolean
  /** When a pointer last went down anywhere on the canvas, or a pinch began: a line's wait for its board ends there. */
  anyDownAt: MutableRefObject<number>
  openBot: (slug: string) => void
  openListed: (path: string) => void
}

export function useHomeLines({ lines, boards, crew, crewNow, drawnBots, inSession, anyDownAt, openBot, openListed }: Inputs) {
  // A press on a line of what waits on the Home surface (`home.ts`, `lineDoes`). THE GRAB (D5): an approval whose
  // board its Bot holds up is taken by the board's own hook, so that same board comes out of Home into his hand, with
  // its one stamp, throw and hand-back. Any other ask opens as its line says: the Bot's own page, which draws that
  // ask's card, or the page of every session's asks.
  const boardViews = useRef(boards.views)
  boardViews.current = boards.views
  // A line takes that ask's own board: its board, or, behind another board of its Bot at the Bot, its own (D24).
  const takeBoard = boards.grab
  const canGrab = useRef(boards.canGrab)
  canGrab.current = boards.canGrab
  const boardsAnswered = useRef(boards.answered)
  boardsAnswered.current = boards.answered
  /**
   * What a press on a line does now. An ask that its Bot will hold up, and that no board shows yet, waits for its
   * board: the Bot is one that holds boards, this is its oldest approval, and no board of that Bot is in the room.
   */
  const drawnNow = useRef(drawnBots)
  drawnNow.current = drawnBots
  const lineDoesNow = useCallback((line: HomeLine) => {
    const views = boardViews.current
    // What a board answered and the list still names gets no second board, and its line does nothing.
    const answered = boardsAnswered.current()
    const bot = line.bot ? crewNow.current.find(b => b.slug === line.bot) : undefined
    const awaited = boardComes(line.id, bot, drawnNow.current.has(line.bot), views.some(v => v.bot === line.bot), answered, boardAskOf)
    return lineDoes(line, views.find(v => v.needId === line.id)?.where, awaited, answered.has(line.id), canGrab.current(line.id), behindTakenBoard(line.id, line.bot, views))
  }, [crewNow])
  /** What a line opens when no board is in play: the Bot's own page, or the page of every session's asks. */
  const openLine = useCallback((line: HomeLine) => {
    if (line.opens === 'bot') openBot(line.bot)
    else if (line.opens === 'view') openListed(HOME_ASKS_PAGE)
  }, [openBot, openListed])
  /** The line that was pressed before its board was made: its board is taken as it comes, within `LINE_WAIT_MS`. */
  const lineWaits = useRef<{ line: HomeLine; at: number; timer: ReturnType<typeof setTimeout> } | null>(null)
  const endWait = useCallback(() => { const w = lineWaits.current; lineWaits.current = null; if (w) clearTimeout(w.timer); return w }, [])
  useEffect(() => () => { endWait() }, [endWait])
  // A session starts or ends: the press that waits was made in the other one, and its page does not open in this one.
  useEffect(() => { endWait() }, [inSession, endWait])
  /** A finger comes down on the desk: a press as a select is, with no select and no pointer event of its own. */
  const touchDown = useCallback(() => { anyDownAt.current = performance.now(); endWait() }, [anyDownAt, endWait])
  const takeWaiting = useCallback((line: HomeLine) => {
    endWait()
    const does = lineDoesNow(line)
    if (does === 'take') takeBoard(line.id)
    else if (does === 'wait') {
      // No board came in that time: the press does what the line opens, unless a board answered the ask meanwhile.
      const at = performance.now()
      const timer = setTimeout(() => { if (endWait() && anyDownAt.current <= at && lineDoesNow(line) !== 'none') openLine(line) }, LINE_WAIT_MS)
      lineWaits.current = { line, at, timer }
    } else if (does !== 'none') openLine(line)
  }, [anyDownAt, endWait, lineDoesNow, openLine, takeBoard])
  // What each line does now, worked out when the lines, the boards or the crew change: Home draws a target by it.
  // eslint-disable-next-line react-hooks/exhaustive-deps -- the boards and the crew stand for what `lineDoesNow` reads.
  const lineDoing = useMemo(() => new Map(lines.map(l => [l.id, lineDoesNow(l)] as const)), [lines, boards.views, crew, drawnBots, lineDoesNow])
  const lineShows = useCallback((line: HomeLine) => lineDoing.get(line.id) ?? 'none', [lineDoing])
  useEffect(() => {
    const w = lineWaits.current
    if (!w) return
    // He pressed something else since: the board does not fly into his hand after he moved on.
    if (anyDownAt.current > w.at) { endWait(); return }
    if (boards.views.some(v => v.needId === w.line.id && v.where === 'bot')) { endWait(); takeBoard(w.line.id) }
  }, [anyDownAt, boards.views, takeBoard, endWait])
  return { lineShows, takeWaiting, touchDown }
}
