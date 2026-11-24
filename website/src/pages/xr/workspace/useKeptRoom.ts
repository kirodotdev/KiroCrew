/**
 * The kept room of the workspace (`Workspace.tsx`): which pages are open and where they stand, the drawn room, the
 * places that a drag gave Bots, the Bot in front, the asks handed back and what the since line keeps, all in one
 * record in the browser's own store (`room.ts`), written a little after each change and when the page is left.
 */
import { useCallback, useEffect, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'
import { Vector3 } from 'three'
import { PAGES, slotOf } from './layout'
import type { PanelProbe } from './Panel'
import { ASK_AGAIN, ROOM_KEY, pagesToAskAgain, readCrew, readFront, readHome, readNotes, readPlaces, readPut, readWalls, roomToKeep, writeRoom, type KeptNotes, type StandingPage } from './room'
import type { Open } from './testHook'
import type { useAskPanels } from './useAskPanels'
import type { DeskState } from './useDesk'
import type { HomeState } from './useHome'
import type { useRooms } from './useRooms'
import type { SinceKept } from './useSince'
import type { TwoBefore } from './twoUp'
import { reachInRoom } from './walls'

/** Where the head is when the workspace opens. The kept room's places are counted round this point (`room.ts`). */
export const START_HEAD = new Vector3(0, PAGES.eyeM, 0)

/** Where a page at `at` stands while the drawn room is on: there, or nearer on the line from the head, in front of the wall (`walls.ts`). */
export function inRoom(head: Vector3, at: Vector3): Vector3 {
  const far = at.distanceTo(head)
  if (far < 1e-6) return at
  const dir = at.clone().sub(head).divideScalar(far)
  const reach = reachInRoom([head.x, head.y, head.z], [dir.x, dir.y, dir.z], far)
  return reach < far - 1e-6 ? dir.multiplyScalar(reach).add(head) : at
}

/** What the kept room reads from the workspace: its pages, their panels, and the hooks made before or after it. */
export interface KeptRoomInputs {
  live: MutableRefObject<Open[]>
  panels: Open[]
  setPanels: Dispatch<SetStateAction<Open[]>>
  readers: MutableRefObject<Map<string, () => PanelProbe>>
  byHand: MutableRefObject<Set<string>>
  /** The pages that stand two in view, with what each goes back to (`twoUp.ts`). */
  twoBefore: MutableRefObject<Map<string, TwoBefore>>
  fromRecord: MutableRefObject<Set<string>>
  asksRef: MutableRefObject<ReturnType<typeof useAskPanels> | null>
  deskRef: MutableRefObject<DeskState | null>
  placedSoon: MutableRefObject<() => void>
  bridge: MutableRefObject<{ head: Vector3 }>
}

export function useKeptRoom({ live, panels, setPanels, readers, byHand, twoBefore, fromRecord, asksRef, deskRef, placedSoon, bridge }: KeptRoomInputs) {
  // The room is kept as it changes: which pages are open, the path each one's tab shows, where each stands round
  // the start head, how big it is, and whether a hand placed it. A little after a change, and when the page is left.
  // A place is kept and read round the same point, so a page comes back to where it stood however often that is done.
  const kept = useRef<string | null>(null)
  // The Home surface (`useHome.ts`), for the callbacks that are made before the hook is called.
  const homeRef = useRef<HomeState | null>(null)
  const standing = useCallback((): StandingPage[] => {
    const pages: StandingPage[] = []
    for (const p of live.current) {
      const path = p.source.pagePath?.()
      // A panel that came up for an ask is kept only once it is the person's page.
      if (!path || asksRef.current?.isAsk(p.id)) continue
      // A page of a pair is kept as the tile put it, its place and size, not as its picture stands now: the glide
      // and the picture at the new size come later than the first write (Y3).
      const two = twoBefore.current.get(p.id)
      const now = two ? undefined : readers.current.get(p.id)?.()
      pages.push({ id: p.id, full: p.source.note?.() === 'full', page: { path, at: slotOf(START_HEAD, now ? new Vector3(...now.position) : p.position), css: now ? now.css : p.css, byHand: byHand.current.has(p.id), ...(two ? { two } : {}) } })
    }
    return pages
  }, [live, asksRef, readers, byHand, twoBefore])
  // A page of the kept room that the gateway had no room for is asked for again, a few times (`room.ts`).
  useEffect(() => {
    const asked = new Map<string, number>()
    const timer = setInterval(() => {
      const again = new Set(pagesToAskAgain(standing(), fromRecord.current, asked))
      for (const p of live.current) if (again.has(p.id)) p.source.askAgain?.()
    }, ASK_AGAIN.everyMs)
    return () => clearInterval(timer)
  }, [standing, fromRecord, live])
  // The drawn room (`RoomWalls.tsx`): a button switches it on, and the record keeps that. It starts off.
  const [walls, setWalls] = useState(() => { try { return readWalls(window.localStorage.getItem(ROOM_KEY)) } catch { return false } })
  const wallsNow = useRef(walls)
  // The places in the row that a drag gave Bots (`crewRow.crewHomes`), kept in the same record.
  const [crewKept, setCrewKept] = useState(() => { try { return readCrew(window.localStorage.getItem(ROOM_KEY)) } catch { return {} } })
  const crewKeptNow = useRef(crewKept)
  crewKeptNow.current = crewKept
  // With no row (`places.ts`): the places that a drag gave Bots, as points, in the same record. The row's yaws above
  // are kept beside them whatever this holds, so a room opened with the row again stands as it stood.
  const [placesKept, setPlacesKept] = useState(() => { try { return readPlaces(window.localStorage.getItem(ROOM_KEY)) } catch { return {} } })
  const placesKeptNow = useRef(placesKept)
  placesKeptNow.current = placesKept
  // The Bots that stand where he put them, with the state each was in then (`room.readPut`).
  const [placesPut, setPlacesPut] = useState(() => { try { return readPut(window.localStorage.getItem(ROOM_KEY)) } catch { return {} } })
  const placesPutNow = useRef(placesPut)
  placesPutNow.current = placesPut
  // The Bot in front with no row, the ask it came for and the look it came to (`room.readFront`).
  const placesFront = useRef((() => { try { return readFront(window.localStorage.getItem(ROOM_KEY)) } catch { return null } })())
  // The asks whose board he handed back: each keeps its Bot at its desk (`room.readHome`).
  const placesHome = useRef<Readonly<Record<string, string>>>((() => { try { return readHome(window.localStorage.getItem(ROOM_KEY)) } catch { return {} } })())
  // His unsent notes on boards, and the boards put by, by ask id (`room.readNotes`, M45).
  const placesNotes = useRef<KeptNotes>((() => { try { return readNotes(window.localStorage.getItem(ROOM_KEY)) } catch { return {} } })())
  // What the line of what changed since he left keeps in the record (`useSince`).
  const sinceKept = useRef<SinceKept | null>(null)
  // A page that the gateway had no room for stays in the record when it came from it (`room.ts`).
  const recordNow = useCallback(() => writeRoom(roomToKeep(standing(), fromRecord.current), wallsNow.current, deskRef.current?.deskNow.current ?? null, crewKeptNow.current, homeRef.current?.pagesHidden ?? false, placesKeptNow.current, placesPutNow.current, placesFront.current, placesHome.current, sinceKept.current?.seen() ?? null, sinceKept.current?.since() ?? null, placesNotes.current), [standing, fromRecord, deskRef])
  // The rooms he keeps (`useRooms.ts`): the record says which one it is, and the list keeps it too.
  const roomsNow = useRef<ReturnType<typeof useRooms> | null>(null)
  const keepRoom = useCallback(() => {
    const text = roomsNow.current ? roomsNow.current.tag(recordNow()) : recordNow()
    if (text === null || text === kept.current) return
    kept.current = text
    try { window.localStorage.setItem(ROOM_KEY, text) } catch { /* no store to write: the room is not kept */ }
    roomsNow.current?.keep(text)
  }, [recordNow])
  const keepSoon = useRef<ReturnType<typeof setTimeout> | null>(null)
  const roomChanged = useCallback(() => {
    // A page that keeps drawing says so many times a second: the first word starts the wait, and the rest ride on it.
    if (keepSoon.current) return
    keepSoon.current = setTimeout(() => { keepSoon.current = null; keepRoom() }, 400)
  }, [keepRoom])
  placedSoon.current = roomChanged
  useEffect(() => {
    if (wallsNow.current === walls) return
    wallsNow.current = walls
    roomChanged()
    if (!walls) return
    // The room comes on: a page that stands behind where the wall is comes to stand in front of it, and stays there.
    setPanels(list => {
      const head = bridge.current.head
      let moved = false
      const next = list.map(p => {
        const now = readers.current.get(p.id)?.()
        const at = now ? new Vector3(...now.position) : p.position
        const to = inRoom(head, at)
        if (to === at) return p
        moved = true
        return { ...p, position: to }
      })
      return moved ? next : list
    })
  }, [walls, roomChanged, bridge, readers, setPanels])
  useEffect(() => {
    roomChanged()
    // A page says when its tab moved on to another path, and when its size is new.
    const quiet = panels.map(p => p.source.subscribe(roomChanged))
    return () => { for (const stop of quiet) stop() }
  }, [panels, roomChanged])
  useEffect(() => {
    window.addEventListener('pagehide', keepRoom)
    // The wait is over too: a wait that is cleared and still named would hold back every later one.
    return () => { window.removeEventListener('pagehide', keepRoom); if (keepSoon.current) { clearTimeout(keepSoon.current); keepSoon.current = null } }
  }, [keepRoom])
  return { kept, homeRef, standing, walls, setWalls, wallsNow, crewKept, setCrewKept, placesKept, setPlacesKept, placesPut, setPlacesPut, placesPutNow, placesFront, placesHome, placesNotes, sinceKept, recordNow, roomsNow, roomChanged }
}
