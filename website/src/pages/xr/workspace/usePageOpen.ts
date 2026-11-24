/**
 * Opening and closing the workspace's pages (`Workspace.tsx`): a new page stands in a free place in front of where
 * he looks, a close mark takes its page away, a window that a page opens gets a panel of its own, the flat preview's
 * picture turns to a page that the plus opened, and the page in front is looked for, whose close mark shows at rest.
 */
import { useCallback, useEffect, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'
import { Vector3 } from 'three'
import { closeSize } from './closeMark'
import { CREW_ZONE } from './crewRow'
import { DomSource, domPagesWork } from './domSource'
import type { NearRef, TurningRef } from './EmptySpace'
import { M_PER_PX, PAGES, placeAt, placeNew, slotOf, type Standing } from './layout'
import { noteText, type PanelProbe } from './Panel'
import { pinchRoute } from './pinchRoute'
import { frontAim, lensFor, nearestAim, shiftAfterTurn, turnTo, type Turn, type View } from './previewLook'
import { MOST_LIVE_PAGES, type PageFocus, type PageNote, type PageSource } from './source'
import { livePath } from './streamPath'
import { StreamSource } from './streamSource'
import type { Open } from './testHook'
import { START_HEAD } from './useKeptRoom'
import { TWO, twoOf, twoPair, twoWay, type TwoBefore } from './twoUp'
import { ROOM_GLIDE_MS } from './useRooms'
import { keysAfter, type KeysPage } from './typing'
import type { useAskPanels } from './useAskPanels'
import type { DeskState } from './useDesk'
import type { HomeState } from './useHome'

/**
 * A live page's source: drawn by this tab itself when that is asked for and the browser can (HTML-in-Canvas,
 * `domSource.ts`), else streamed by the gateway. Also in a session: there the session's pointers (the dot and a
 * raised hand's ray) hand their presses in (`domAim.ts`), and a page whose paint stops behind the session goes to the
 * stream by itself (`handOver` in the workspace).
 */
export function liveSourceFor(dom: boolean, id: string, path: string, cssWidth: number, cssHeight: number): PageSource {
  return dom && domPagesWork() ? new DomSource(id, path, cssWidth, cssHeight) : new StreamSource(id, path, cssWidth, cssHeight)
}

/** How often the page in front is looked for, whose close mark shows at rest (ms). */
const FRONT_EVERY_MS = 200
/**
 * A window that a page opens (`PageSource.windows`) turns the picture to it only when he pressed something this
 * recently (ms); one that came by itself stands by its page, and takes no press for `WINDOW_CALM_MS`.
 */
const WINDOW_PRESS_MS = 1000
/** A window that came by itself takes no press this long (ms). */
const WINDOW_CALM_MS = 1000

/** What opening and closing pages reads from the workspace: the pages and their panels, and the hooks around it. */
export interface PageOpenInputs {
  bridge: MutableRefObject<{ head: Vector3; lookYawDeg: number; inSession: boolean; view: View }>
  panels: Open[]
  setPanels: Dispatch<SetStateAction<Open[]>>
  live: MutableRefObject<Open[]>
  readers: MutableRefObject<Map<string, () => PanelProbe>>
  byHand: MutableRefObject<Set<string>>
  /** The pages that stand two in view, with what each goes back to (`twoUp.ts`). The room's record keeps it. */
  twoBefore: MutableRefObject<Map<string, TwoBefore>>
  fromRecord: MutableRefObject<Set<string>>
  crossed: MutableRefObject<Set<string>>
  notes: MutableRefObject<Map<string, PageNote>>
  focusAt: MutableRefObject<Map<string, PageFocus>>
  pressed: MutableRefObject<KeysPage | null>
  lookFor: MutableRefObject<{ opened: string | null; closed: boolean }>
  deskRef: MutableRefObject<DeskState | null>
  asksRef: MutableRefObject<ReturnType<typeof useAskPanels> | null>
  homeRef: MutableRefObject<HomeState | null>
  hush: (closed: boolean) => void
  settleField: () => void
  say: (words: string, top?: boolean) => void
  inXr: () => boolean
  wantsDom: boolean
  /** A page of this path is about to be asked for: the warm rooms make room for it (`useWarmRooms.ts`). */
  room?: (path: string) => void
  turn: MutableRefObject<Turn>
  shift: MutableRefObject<number>
  near: NearRef
  turning: TurningRef
}

export function usePageOpen({ bridge, panels, setPanels, live, readers, byHand, twoBefore, fromRecord, crossed, notes, focusAt, pressed, lookFor, deskRef, asksRef, homeRef, hush, settleField, say, inXr, wantsDom, room, turn, shift, near, turning }: PageOpenInputs) {
  /**
   * Stand a new page among the open ones: in a free place where it crosses none of them. A page that a hand has
   * placed never moves for it. Where each page is now is read from its panel, since a hand may have moved it.
   */
  const standNew = useCallback((list: Open[], id: string, source: PageSource, css: [number, number], nearYawDeg = bridge.current.lookYawDeg): Open[] => {
    const head = bridge.current.head
    const standing: Standing[] = list.map(p => {
      const now = readers.current.get(p.id)?.()
      return {
        id: p.id, user: byHand.current.has(p.id), at: slotOf(head, now ? new Vector3(...now.position) : p.position),
        widthM: now ? now.size[0] : p.css[0] * M_PER_PX, heightM: now ? now.size[1] : p.css[1] * M_PER_PX,
      }
    })
    const deskZone = deskRef.current?.zone() ?? null
    const got = placeNew(standing, css[0] * M_PER_PX, css[1] * M_PER_PX, nearYawDeg, deskZone ? [CREW_ZONE, deskZone] : [CREW_ZONE])
    if (got.crosses) crossed.current.add(id); else crossed.current.delete(id)
    const moved = new Map(got.moved.map(m => [m.id, m.at]))
    const stood = list.map(p => { const to = moved.get(p.id); return to ? { ...p, position: placeAt(head, to.yawDeg, to.pitchDeg, to.distanceM) } : p })
    return [...stood, { id, source, css, position: placeAt(head, got.at.yawDeg, got.at.pitchDeg, got.at.distanceM) }]
  }, [bridge, byHand, crossed, deskRef, readers])

  /** Take panels away: their pages are closed, and what was kept of them goes with them. */
  /** A page of a pair goes back where and as big as it was before the pair (`twoUp.ts`): its panel, and its hand's mark. */
  const twoBack = useCallback((p: Open, was: TwoBefore): Open => {
    if (was.byHand) byHand.current.add(p.id); else byHand.current.delete(p.id)
    if (p.css.join() !== was.css.join()) p.source.resize(...was.css)
    return { ...p, css: was.css, position: placeAt(START_HEAD, was.at.yawDeg, was.at.pitchDeg, was.at.distanceM), glide: true }
  }, [byHand])
  // A glide the tile asks for ends: the next place a page is given (a room, an open) is not glided to by mistake (Y5).
  const glideDone = useCallback(() => { setTimeout(() => setPanels(list => (list.some(p => p.glide) ? list.map(p => (p.glide ? { ...p, glide: false } : p)) : list)), ROOM_GLIDE_MS + 200) }, [setPanels])
  const drop = useCallback((gone: (p: Open) => boolean) => {
    setPanels(list => {
      // One page of a pair goes: the other gets its own size and place back in the same step.
      const pairEnds = list.some(p => gone(p) && twoBefore.current.has(p.id))
      for (const p of list) if (gone(p)) { byHand.current.delete(p.id); fromRecord.current.delete(p.id); crossed.current.delete(p.id); notes.current.delete(p.id); focusAt.current.delete(p.id) }
      const left = list.filter(p => !gone(p))
      if (!pairEnds) return left
      const was = new Map(twoBefore.current)
      twoBefore.current.clear()
      glideDone()
      return left.map(p => { const back = was.get(p.id); return back ? twoBack(p, back) : p })
    })
  }, [byHand, crossed, focusAt, fromRecord, notes, setPanels, twoBefore, twoBack, glideDone])
  /**
   * Two in view (D66): the page ahead of him and the page he opened last stand side by side in the lens; asked
   * again, both go back where and as big as they were. In a session only: the flat views have no lens to fill.
   */
  const [twoOn, setTwoOn] = useState(() => twoBefore.current.size === 2)
  // The record brings a pair with a room (a load, a switch), and a close ends one: the tile's word follows the pair.
  // A pair is two pages: one kept page of a pair (its other gone from the record) is no pair, and what it kept of
  // before is let go where it stands (Y1).
  useEffect(() => {
    if (twoBefore.current.size === 1) twoBefore.current.clear()
    const on = twoBefore.current.size === 2
    if (on !== twoOn) setTwoOn(on)
  }, [panels, twoOn, twoBefore])
  /**
   * The hand wins over the pair (D70 2): a carry or a size of a page of the pair ends it. That page keeps the hand's
   * place and size, the other stays where it stands, and nothing jumps; the tile reads "Two in view" again.
   */
  const twoHand = useCallback((id: string) => {
    if (!twoBefore.current.has(id)) return
    twoBefore.current.clear()
    setTwoOn(false)
  }, [twoBefore])
  const twoToggle = useCallback(() => {
    if (!bridge.current.inSession) return
    if (twoBefore.current.size === 2) {
      const was = new Map(twoBefore.current)
      twoBefore.current.clear()
      setPanels(list => list.map(p => { const back = was.get(p.id); return back ? twoBack(p, back) : p }))
      setTwoOn(false)
      glideDone()
      return
    }
    twoBefore.current.clear()
    const now = (p: Open) => { const read = readers.current.get(p.id)?.(); return { at: slotOf(START_HEAD, read ? new Vector3(...read.position) : p.position), css: (read ? read.css : p.css) as [number, number] } }
    const open = live.current.filter(p => !asksRef.current?.isAsk(p.id))
    const ids = twoOf(open.map(p => ({ id: p.id, yawDeg: now(p).at.yawDeg })), bridge.current.lookYawDeg)
    if (!ids) return
    // Where he faces at the press (his head's yaw, level, as a board is taken), when that is more than half the lens
    // from the room's front; else the room's front, under Home's strip (D80 3). Never round a page away from his look.
    const facesDeg = ((bridge.current.lookYawDeg + 540) % 360) - 180
    const pair = twoPair(twoWay(window.location.search), Math.abs(facesDeg) > TWO.lensDeg / 2 - 0.5 ? facesDeg : 0)
    for (const p of open) if (ids.includes(p.id)) twoBefore.current.set(p.id, { ...now(p), byHand: byHand.current.has(p.id) })
    setPanels(list => list.map(p => {
      const to = pair[ids.indexOf(p.id)]
      if (!to) return p
      if (p.css.join() !== to.css.join()) p.source.resize(...to.css)
      return { ...p, css: to.css, position: placeAt(START_HEAD, to.at.yawDeg, to.at.pitchDeg, to.at.distanceM), glide: true }
    }))
    setTwoOn(true)
    glideDone()
  }, [bridge, byHand, live, readers, asksRef, setPanels, twoBefore, twoBack, glideDone])
  // The gateway has the last word on what a panel may show, and this page asks for more than its guard lets
  // through (`streamPath.ts`). A page that it refuses to open leaves no panel behind.
  useEffect(() => {
    // The panel goes as by its close mark: what stood behind it is not what a finger was aimed at, so the quiet time starts.
    const stops = panels.map(p => p.source.subscribe(() => { if (p.source.refusedOpen?.()) { asksRef.current?.refused(p.id); hush(true); drop(q => q.id === p.id) } }))
    return () => { for (const stop of stops) stop() }
  }, [panels, drop, hush, asksRef])
  /** The close mark of a panel: its page goes, and the gateway closes its tab. */
  const closePage = useCallback((id: string) => {
    pressed.current = keysAfter(pressed.current, { closed: id })
    // The quiet time starts with the close, whether or not the picture then moves: what stood behind the mark is
    // not what the finger was aimed at either.
    // An ask's panel came up with no turn, and goes with none: the picture is where it was.
    if (!bridge.current.inSession && !asksRef.current?.isAsk(id)) lookFor.current.closed = true
    hush(true)
    asksRef.current?.closedByMark(id)
    drop(p => p.id === id)
    settleField()
  }, [drop, settleField, hush, asksRef, bridge, lookFor, pressed])
  /** The palette, or the plus: a page of the list opens on a new panel, in front of where the person looks. */
  const opened = useRef(0)
  /**
   * `by`: a window that a page opened by itself (no press of his in the last second): it stands by that page, the
   * picture does not turn to it, and it takes no press in its first second.
   */
  const openPage = useCallback((path: string, by?: { nearYawDeg: number }) => {
    const page = livePath(path)
    if (!page) return
    if (!by) asksRef.current?.tapped()
    opened.current += 1
    const id = `open-${opened.current}`
    const css: [number, number] = [PAGES.cssWidth, PAGES.cssHeight]
    room?.(page)
    const source = liveSourceFor(wantsDom, id, page, css[0], css[1])
    // A page that is one too many is refused by the gateway: the picture does not turn to it.
    if (!by && !bridge.current.inSession && live.current.length < MOST_LIVE_PAGES) lookFor.current.opened = id
    if (by) { calmUntil.current.set(id, performance.now() + WINDOW_CALM_MS); source.holdFor?.(WINDOW_CALM_MS) }
    setPanels(list => standNew(list, id, source, css, by?.nearYawDeg))
    return { id, source }
  }, [standNew, wantsDom, room, asksRef, bridge, live, lookFor, setPanels])
  /** Until when a panel takes no press: a window that came by itself, in its first second. */
  const calmUntil = useRef(new Map<string, number>())
  /** When he last pressed anything: a page, the empty room, a pinch. */
  const pressAt = useRef(-Infinity)
  // A window that a page opens: on a panel of its own (the open rule was asked by the source and the gateway).
  useEffect(() => {
    for (const p of panels) p.source.windows?.(path => {
      if (live.current.length >= MOST_LIVE_PAGES) { say(noteText('full')); return }
      if (performance.now() - pressAt.current < WINDOW_PRESS_MS) { openPage(path); return }
      const read = readers.current.get(p.id)?.()
      openPage(path, { nearYawDeg: slotOf(bridge.current.head, read ? new Vector3(...read.position) : p.position).yawDeg })
    })
    return () => { for (const p of panels) p.source.windows?.(null) }
  }, [panels, openPage, say, bridge, live, readers])
  /**
   * Turn the picture to a page round the head. `closed`: a close began it, so the quiet time is the close's. The
   * frame's shift goes with the turn, to where the page's upper edge and its close mark are in the picture
   * (`shiftAfterTurn`); the head stays level with the page. Returns where the head looks when the turn is done.
   */
  const turnPicture = useCallback((aim: { yawDeg: number; pitchDeg: number; distanceM: number; heightM: number }, closed: boolean): Turn => {
    const to = turnTo(turn.current, aim)
    const view = bridge.current.view
    // The close mark's square over the page's upper edge, as the panel sizes it (`Panel.tsx`, `closeSize`).
    const edge = Math.round(aim.distanceM * Math.tan((PAGES.edgeDeg * Math.PI) / 180) * 200) / 200
    const markAt = (fovDeg: number) => Math.ceil(closeSize(edge, (2 * aim.distanceM * Math.tan((fovDeg * Math.PI) / 360)) / Math.max(1, view.height)).press * 200) / 200
    const markM = markAt(view.fovDeg)
    const shifted = { from: shift.current, to: shiftAfterTurn(shift.current, near.current, view, aim, markM, markAt(lensFor(view.width, view.height, 1))) }
    // The pages move under the finger from now on, in a turn as in a jump: the quiet time runs from here.
    hush(closed)
    // The page that had the keys leaves the picture: the keys are no page's, as after a press on empty space.
    pressed.current = keysAfter(pressed.current, { away: true })
    settleField()
    // With reduced motion set, the turn is a jump.
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) { turning.current = null; turn.current = to; shift.current = shifted.to; return to }
    turning.current = { from: turn.current, to, t0: performance.now(), shift: shifted }
    return to
  }, [hush, settleField, bridge, near, pressed, shift, turn, turning])
  /** The panel that the picture last turned to because it was opened, and where that turn leaves the head. */
  const turnedFor = useRef<{ id: string; to: Turn } | null>(null)
  // In the flat preview the picture turns to a page that the plus opened, and to the nearest page that is left
  // when a close leaves no page in the picture (`previewLook.ts`). Nothing else turns it: not the kept room, not
  // the page that the address asks for, not a page that a test opens. In a session the person turns the head.
  useEffect(() => {
    const want = lookFor.current
    lookFor.current = { opened: null, closed: false }
    if (inXr() || (!want.opened && !want.closed)) return
    const head = bridge.current.head
    const aims = panels.filter(p => !homeRef.current?.hides(p.id)).map(p => { const now = readers.current.get(p.id)?.(); return { id: p.id, heightM: now ? now.size[1] : p.css[1] * M_PER_PX, ...slotOf(head, now ? new Vector3(...now.position) : p.position) } })
    const aim = want.opened ? aims.find(a => a.id === want.opened) ?? null : nearestAim(turn.current, bridge.current.view, aims)
    if (!aim) return
    const to = turnPicture(aim, !want.opened)
    if (want.opened) turnedFor.current = { id: aim.id, to }
  }, [panels, inXr, turnPicture, homeRef, bridge, lookFor, readers, turn])
  // The page in front, whose close mark shows at rest (`Panel.tsx`, `markAtRest`): in the flat preview the page
  // nearest the picture's middle; in a session the page that the one light lies on (the dot, or a raised hand's
  // ray), its frame and its mark included, and none while the light is on nothing.
  const [front, setFront] = useState<string | null>(null)
  useEffect(() => {
    const look = () => {
      let id: string | null = null
      if (inXr()) {
        const on = /^(?:page|panel):(.+)$/.exec(pinchRoute.lit())?.[1] ?? null
        id = on && live.current.some(p => p.id === on) ? on : null
      } else {
        const head = bridge.current.head
        const aims = live.current.map(p => { const now = readers.current.get(p.id)?.(); return { id: p.id, ...slotOf(head, now ? new Vector3(...now.position) : p.position) } })
        id = frontAim(turning.current ? turning.current.to : turn.current, bridge.current.view, aims)?.id ?? null
      }
      setFront(now => (now === id ? now : id))
    }
    look()
    const every = window.setInterval(look, FRONT_EVERY_MS)
    return () => window.clearInterval(every)
  }, [inXr, bridge, live, readers, turn, turning])
  /**
   * A row of the plus. A panel keeps two addresses: the one it was opened with (`openedWith`, the row's own when a
   * row opened it), and the page's real one now (`pagePath`, path and query). A row turns the picture to a panel
   * that it opened itself, wherever the page went by itself since (the Bots page moves on to the Bot it showed
   * last); else to a panel whose real address now is the row's (the chat of another session, the page of another
   * Bot, are other pages); else it opens. A second press on the same row while the picture still looks where that
   * row's turn left it opens a second copy beside it. In a session the person turns the head, so the row opens.
   */
  const openedWith = useRef(new Map<string, string>())
  const listedTurn = useRef<{ id: string; to: Turn } | null>(null)
  const openListed = useCallback((path: string) => {
    const open = () => { const made = openPage(path); if (made) openedWith.current.set(made.id, path) }
    const shown = live.current.find(p => openedWith.current.get(p.id) === path) ?? live.current.find(p => p.source.pagePath?.() === path)
    // A page that is put away and asked for by its row is taken out again.
    if (shown) homeRef.current?.reveal(shown.id)
    if (!shown || inXr()) { open(); return }
    const made = listedTurn.current?.id === shown.id ? listedTurn.current.to : null
    const now = turning.current ? turning.current.to : turn.current
    if (made && Math.abs(now.yawDeg - made.yawDeg) < 1e-6 && Math.abs(now.pitchDeg - made.pitchDeg) < 1e-6) { open(); return }
    const read = readers.current.get(shown.id)?.()
    const to = turnPicture({ heightM: read ? read.size[1] : shown.css[1] * M_PER_PX, ...slotOf(bridge.current.head, read ? new Vector3(...read.position) : shown.position) }, false)
    listedTurn.current = { id: shown.id, to }
  }, [inXr, openPage, turnPicture, homeRef, bridge, live, readers, turn, turning])
  return { standNew, drop, closePage, opened, openPage, calmUntil, pressAt, turnPicture, turnedFor, front, openListed, twoOn, twoToggle, twoHand }
}
