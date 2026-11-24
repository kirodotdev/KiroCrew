/**
 * The desk of the workspace (`desk.ts`), as state: where it stands, the tiles that lie on it, the rows of the plus's
 * list, and what a press on a tile does. Only with `desk=1` in the address; without it the workspace never asks.
 *
 * Where it stands: in the flat preview the desk that the room's record keeps, else the default place. In a session
 * a desk that a session kept, shown dim until a press lands on it; with none kept, the default place. A desk kept in
 * the flat preview (which has no real height) is never used in a session.
 */
import { useQuery } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject } from 'react'
import type { Vector3 } from 'three'
import { api } from '../../../api/client'
import { i18nT } from '../../../i18n/t'
import { DESK, defaultDesk, deskForSession, deskPoint, deskZone, seatOrder, swapSeat, type DeskPlace } from './desk'
import { movesDesk } from './deskSetup'
import type { DeskRow, DeskTile } from './Desk'
import type { Zone } from './layout'
import { menuLabel, pagesToOpen, PAGE_MENU } from './pageMenu'
import { usePanelWay } from './panelWay'
import { ROOM_KEY, readDesk } from './room'

type V3 = [number, number, number]

export interface DeskHost {
  /** The head now, and whether a session runs. */
  head: () => Vector3
  inSession: boolean
  /** Opens a page as the bar's plus does: a panel that shows it already is turned to instead (`openListed`). */
  openListed: (path: string) => void
  startView: () => void
  /** The room's record is to be written again: the desk's place is in it. */
  roomChanged: () => void
  /** The new chat card comes out (`chatCard.ts`). */
  newChat?: () => void
}

export interface DeskState {
  desk: DeskPlace | null
  deskNow: MutableRefObject<DeskPlace | null>
  tiles: DeskTile[]
  rows: DeskRow[]
  onTile: (id: string) => void
  onRow: (path: string) => void
  /** The part of the view that a page that opens keeps out of, or none. */
  zone: () => Zone | null
  /** Points of the desk's top, for an ask's panel to stand clear of (`asks.ts`, `askPlace`). */
  points: () => V3[] | undefined
  /** A press landed on the desk: a kept desk that was a guess is taken as right. */
  confirm: () => void
  /** Which Bot sits where (`seatOrder`): the seats by slug ('' for an empty one), and the Bots past the last seat. */
  seating: { seats: string[]; more: string[] }
  /** A Bot was put down on seat `to`: it sits there, and the Bot that sat there takes its seat (`swapSeat`). */
  moveSeat: (slug: string, to: number) => void
  /** When the set-up by flat hands was armed, or `null` (`DeskSetup.tsx`); and its end, with the place the hands set. */
  armedAt: number | null
  setupDone: (place: { at: V3; yawDeg: number } | null) => void
}

/** The Bots page, which the others' pad opens: the page of `PAGE_MENU` with every Bot. */
const BOTS_PAGE = '/members'

const kept = (): DeskPlace | null => { try { return readDesk(window.localStorage.getItem(ROOM_KEY)) } catch { return null } }
const eyeOf = (v: Vector3): V3 => [v.x, v.y, v.z]

/**
 * `slugs` are the crew's Bots in the crew's order, and `known` says the roster was read whole: only then does a seat
 * that a Bot left stand empty and a new Bot take one, so that a read that is not in yet moves nobody.
 */
export function useDesk(on: boolean, host: DeskHost, slugs: readonly string[], known: boolean): DeskState {
  const way = usePanelWay()
  const { data: config } = useQuery({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig(), enabled: on })
  const [desk, setDesk] = useState<DeskPlace | null>(() => (on ? kept() ?? defaultDesk(eyeOf(host.head())) : null))
  // A Bot keeps its own seat: the order is kept with the desk in the room's record.
  const [seats, setSeats] = useState<readonly string[]>(() => kept()?.seats ?? [])
  const slugsKey = slugs.join('\n')
  // eslint-disable-next-line react-hooks/exhaustive-deps -- `slugsKey` stands for `slugs`, which is made anew at each render.
  const seating = useMemo(() => seatOrder(seats, slugs), [seats, slugsKey])
  useEffect(() => {
    if (on && known && slugs.length > 0 && seating.seats.join('\n') !== seats.join('\n')) setSeats(seating.seats)
  }, [on, known, slugs.length, seating, seats])
  const deskNow = useRef<DeskPlace | null>(desk)
  deskNow.current = desk ? { ...desk, seats } : null
  const hostNow = useRef(host)
  hostNow.current = host
  // A session starts or ends: the desk of that place. A desk that a session kept is a guess there until a press lands on it.
  useEffect(() => {
    if (!on) return
    if (host.inSession) setDesk(deskForSession(kept()) ?? defaultDesk(eyeOf(hostNow.current.head()), 0, true))
    else setDesk(kept() ?? defaultDesk(eyeOf(hostNow.current.head())))
  }, [on, host.inSession])
  useEffect(() => { if (on && desk) hostNow.current.roomChanged() }, [on, desk, seats])
  // The set-up by flat hands is armed when a session starts (a session's desk is never confirmed at its start), and
  // by the tile "set the desk" in a session; at no other time do flat hands move the desk.
  const [armedAt, setArmedAt] = useState<number | null>(null)
  useEffect(() => { setArmedAt(on && host.inSession ? performance.now() : null) }, [on, host.inSession])
  const setupDone = useCallback((place: { at: V3; yawDeg: number } | null) => {
    setArmedAt(null)
    if (!place) return
    setDesk(d => (movesDesk(d, place) ? { at: place.at, yawDeg: place.yawDeg, from: 'hands', inSession: true, confirmed: true } : d && { ...d, confirmed: true }))
  }, [])

  const tiles = useMemo<DeskTile[]>(() => {
    if (!on) return []
    // One row, in this order; the start view is the flat preview's only, and its place stays free in a session.
    const out: DeskTile[] = [
      { id: 'plus', label: i18nT('xr.open_page') },
      { id: 'chat', label: menuLabel(PAGE_MENU[0]) },
    ]
    if (!host.inSession) out.push({ id: 'start', label: i18nT('xr.start_view') })
    // The new chat card's tile takes the place that the start view leaves free in a session.
    else out.push({ id: 'card', label: i18nT('pages.chat.sessionFlyout.new_chat') })
    out.push({ id: 'desk', label: i18nT('xr.desk_set') })
    return out
  }, [on, host.inSession])
  const rows = useMemo<DeskRow[]>(() => (on ? pagesToOpen(way, config).map(p => ({ path: p.path, label: menuLabel(p) })) : []), [on, way, config])

  const onTile = useCallback((id: string) => {
    const h = hostNow.current
    if (id === 'chat') h.openListed(PAGE_MENU[0].path)
    else if (id === 'more') h.openListed(BOTS_PAGE)
    else if (id === 'start') h.startView()
    else if (id === 'card') h.newChat?.()
    // In a session the tile arms the set-up by flat hands; in the flat preview, which has no hands, it puts the desk back at its default place.
    else if (id === 'desk') { if (h.inSession) setArmedAt(performance.now()); else setDesk(defaultDesk(eyeOf(h.head()))) }
  }, [])
  const onRow = useCallback((path: string) => { hostNow.current.openListed(path) }, [])
  const zone = useCallback(() => (deskNow.current ? deskZone(deskNow.current, eyeOf(hostNow.current.head())) : null), [])
  const points = useCallback(() => {
    const d = deskNow.current
    if (!d) return undefined
    const out: V3[] = []
    for (let i = 0; i <= 10; i += 1) for (let j = 0; j <= 5; j += 1) out.push(deskPoint(d, -DESK.widthM / 2 + (i * DESK.widthM) / 10, (j * DESK.depthM) / 5, DESK.botM))
    return out
  }, [])
  const confirm = useCallback(() => { setDesk(d => (d && !d.confirmed ? { ...d, confirmed: true } : d)) }, [])
  const seatingNow = useRef(seating)
  seatingNow.current = seating
  const moveSeat = useCallback((slug: string, to: number) => { setSeats(swapSeat(seatingNow.current.seats, slug, to)) }, [])
  return { desk, deskNow, tiles, rows, onTile, onRow, zone, points, confirm, seating, moveSeat, armedAt, setupDone }
}
