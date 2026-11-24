/**
 * The Home surface (`home.ts`), as state: its four numbers and the lines of what waits, from the crew that the room
 * has already; its form; and the switch that puts the real pages away and brings them back.
 *
 * Put away, a page is still open: it keeps its tab at the gateway, its place and its size, and the room's record
 * keeps it as before. Only its panel is not drawn, and its source is told that nobody sees it, so no picture is
 * made for it. The switch puts away the pages that stand at that moment. A page that comes after it shows: one that
 * he opens to read a detail, and a Bot's page that comes up for an ask. The record keeps the switch
 * (`room.ts`, `pagesHidden`); the workspace then opens with its kept pages put away, except the page that the
 * address asks for. A room with no record kept opens so too (`pagesHiddenAtStart`): Home is the main panel.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { XrBot, XrNeed } from '../model'
import { doneLines, doneNow, homeForm, homeLines, homeStats, keepDone, type DoneBook, type DoneLine, type HomeForm, type HomeLine, type HomeStats } from './home'
import { toRead } from './says'
import { ROOM_KEY, pagesHiddenAtStart } from './room'
import type { PageSource } from './source'

export interface HomeHost {
  /** The open pages now. */
  panels: () => readonly { id: string; source: PageSource }[]
  /** Whether a panel came up for an ask and is not yet the person's own page: such a panel is never put away. */
  isAsk: (id: string) => boolean
  /** Each page's place and size, as its panel has them now, go into the list of open pages: a panel that is not drawn cannot say them. */
  settle: () => void
  /** The keyboard is no page's from here on. */
  away: () => void
  /** What stood under the pointer changes: the quiet time starts, as after a close. */
  hush: () => void
  /** The room's record is to be written again. */
  roomChanged: () => void
  /** The clock that "today" is read from. */
  now: () => number
  /** The address asks for a page (`?page=`): that page shows also when the kept pages are put away. */
  asked: boolean
  /** The address says `pages=show`: a room with no record kept opens with its pages showing (`pagesHiddenAtStart`). */
  showWord: boolean
}

export interface HomeState {
  /** The switch: the real pages are put away. */
  pagesHidden: boolean
  /** Whether this page is put away now. */
  hides: (id: string) => boolean
  /** Take one page out again, for a press that asks for it. */
  reveal: (id: string) => void
  toggle: () => void
  /** Put the pages away, or show them all: a room he goes to says which (`useRooms.ts`). */
  setAway: (on: boolean) => void
  /** The four numbers, or `null` while the Bots' states cannot be known: a number that may be wrong is not shown. */
  stats: HomeStats | null
  lines: HomeLine[]
  more: number
  form: HomeForm
  /** "Done today" (D35): the newest lines of what he filed, and how many answers wait at their Bots to be read. */
  done: DoneLine[]
  toRead: number
}

// A browser with no store keeps no room: it opens as a room with no record does.
const kept = (showWord: boolean): boolean => { try { return pagesHiddenAtStart(window.localStorage.getItem(ROOM_KEY), showWord) } catch { return !showWord } }

/**
 * `panelIds` are the open pages, `bots` the crew as the room draws it, `needs` every ask that waits, and `known`
 * says that the states can be known now.
 */
/**
 * A page that was put away is shown again: its last picture may be old (or the flat first paint it got before its
 * hide took), so it is taken back as a warm page is: dimmed, taking no press, until its fresh picture (D49; M6).
 */
const showAgain = (source: PageSource) => { if (source.takenBack) source.takenBack(); else source.visible?.(true) }

export function useHome(host: HomeHost, panelIds: readonly string[], bots: readonly XrBot[], needs: readonly XrNeed[], known: boolean): HomeState {
  const hostNow = useRef(host)
  hostNow.current = host
  const [pagesHidden, setPagesHidden] = useState(() => kept(host.showWord))
  const [hidden, setHidden] = useState<ReadonlySet<string>>(() => new Set(pagesHidden ? host.panels().map(p => p.id).filter(id => !(host.asked && id === 'page')) : []))
  const hiddenNow = useRef(hidden)
  hiddenNow.current = hidden
  const awayNow = useRef(pagesHidden)
  awayNow.current = pagesHidden
  // The pages that open put away make no picture from the start.
  useEffect(() => {
    for (const p of hostNow.current.panels()) if (hiddenNow.current.has(p.id)) p.source.visible?.(false)
  }, [])
  const first = useRef(true)
  useEffect(() => {
    if (first.current) { first.current = false; return }
    hostNow.current.roomChanged()
  }, [pagesHidden])

  // `on`: the pages that stand now are put away (those already away stay so); off, they all show again.
  const setAway = useCallback((on: boolean) => {
    const h = hostNow.current
    h.hush()
    if (on) {
      h.settle()
      h.away()
      const away = h.panels().filter(p => !h.isAsk(p.id))
      for (const p of away) p.source.visible?.(false)
      setHidden(new Set(away.map(p => p.id)))
      setPagesHidden(true)
      return
    }
    for (const p of h.panels()) if (hiddenNow.current.has(p.id)) showAgain(p.source)
    setHidden(new Set())
    setPagesHidden(false)
  }, [])
  const toggle = useCallback(() => setAway(!awayNow.current), [setAway])
  const reveal = useCallback((id: string) => {
    if (!hiddenNow.current.has(id)) return
    const p = hostNow.current.panels().find(q => q.id === id)
    if (p) showAgain(p.source)
    setHidden(was => new Set([...was].filter(x => x !== id)))
  }, [])
  const hides = useCallback((id: string) => hidden.has(id), [hidden])

  // "Today" moves on at midnight: the numbers are counted again each minute.
  const [minute, setMinute] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => setMinute(n => n + 1), 60_000)
    return () => clearInterval(timer)
  }, [])
  // What was counted as done today stays counted until the day turns (`keepDone`): the number does not go down when
  // a Bot that had finished starts again. The book lives as long as the page; a reload counts anew from what rests.
  const [book, setBook] = useState<DoneBook | null>(null)
  // eslint-disable-next-line react-hooks/exhaustive-deps -- `minute` stands for the clock.
  const counted = useMemo(() => (known ? keepDone(book, doneNow(bots, hostNow.current.now()), hostNow.current.now()) : book), [known, bots, book, minute])
  useEffect(() => { if (counted !== book) setBook(counted) }, [counted, book])
  // eslint-disable-next-line react-hooks/exhaustive-deps -- `minute` stands for the clock.
  const stats = useMemo(() => (known ? homeStats(bots, needs, hostNow.current.now(), counted?.ids) : null), [known, bots, needs, counted, minute])
  const { lines, more } = useMemo(() => (known ? homeLines(needs) : { lines: [], more: 0 }), [known, needs])
  // eslint-disable-next-line react-hooks/exhaustive-deps -- `minute` stands for the clock.
  const done = useMemo(() => (known ? doneLines(bots, hostNow.current.now()) : []), [known, bots, minute])
  const unread = useMemo(() => (known ? toRead(bots) : 0), [known, bots])
  const shown = panelIds.filter(id => !hidden.has(id)).length
  return { pagesHidden, hides, reveal, toggle, setAway, stats, lines, more, form: homeForm(shown), done, toRead: unread }
}
