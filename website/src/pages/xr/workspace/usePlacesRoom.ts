/**
 * The room with no row (`places.ts`), as the workspace hands it to the crew: the pages that show as the eye sees
 * them, what holds the low middle, where he put Bots, which Bot stands in front and which he sent home; and Home as
 * it is shown there (its strip, large on a press). One hook, so that the workspace only names what it reads.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'
import type { Quaternion, Vector3 } from 'three'
import type { XrBot } from '../model'
import type { PlacesRoom } from './Crew'
import { M_PER_PX } from './layout'
import { seenBox, type KeptPlaces, type Pt, type SeenBox } from './places'
import type { KeptFront } from './room'
import { keysSize, type useChatCard } from './useChatCard'
import type { HomeState } from './useHome'

export interface PlacesRoomHost {
  placesOn: boolean
  panels: readonly { id: string; position: Vector3; css: [number, number] }[]
  /** During a switch of rooms: the pages of the room he goes to, where they will stand. The crew reads these, not the pages that still stand. */
  ahead?: readonly { position: Vector3; css: [number, number] }[] | null
  home: HomeState
  crew: readonly XrBot[]
  statesKnown: boolean
  placesKept: KeptPlaces
  placesPut: Readonly<Record<string, string>>
  setPlacesKept: Dispatch<SetStateAction<KeptPlaces>>
  setPlacesPut: Dispatch<SetStateAction<Record<string, string>>>
  placesPutNow: MutableRefObject<Readonly<Record<string, string>>>
  crewNow: MutableRefObject<readonly XrBot[]>
  placedSoon: MutableRefObject<() => void>
  bridge: MutableRefObject<{ lookYawDeg: number; quat: Quaternion }>
  placesFront: MutableRefObject<KeptFront | null>
  placesHome: MutableRefObject<Readonly<Record<string, string>>>
  /** The asks whose board he handed back (`useBoards`, `backAsks`). */
  backAsks: Readonly<Record<string, string>>
  /** The new chat card's keyboard, when it stands (`useChatCard`, `keys`). */
  cardKeys: ReturnType<typeof useChatCard>['keys']
  /** A board is in his hand, or he holds a Bot to speak to it: the middle of his look is quiet from then on. */
  handBusy: boolean
}

export function usePlacesRoom({ placesOn, panels, ahead = null, home, crew, statesKnown, placesKept, placesPut, setPlacesKept, setPlacesPut, placesPutNow, crewNow, placedSoon, bridge, placesFront, placesHome, backAsks, cardKeys, handBusy }: PlacesRoomHost) {
  // With no row (`places.ts`): what the crew reads of the room. The pages that show, as the eye sees them; what holds
  // the low middle (a taken tray, the card's keyboard); and whether Home's large surface has the middle.
  const [trayTaken, setTrayTaken] = useState(false)
  const [forward, setForward] = useState(false)
  const homeHides = home.hides
  const pageBoxes = useMemo<SeenBox[]>(() => (placesOn ? (ahead ?? panels.filter(p => !homeHides(p.id))).map(p => seenBox(p.position, p.css[0] * M_PER_PX, p.css[1] * M_PER_PX)) : []), [placesOn, panels, ahead, homeHides])
  // With no row Home stands as its strip, so that the Bots have the middle of the view. A press on the strip opens
  // its large form, and a press on that puts it back; a Bot that comes forward, or a page, puts it back too.
  const [homeOpen, setHomeOpen] = useState(false)
  const homeLarge = !placesOn ? home.form === 'large' : home.form === 'large' && homeOpen && !forward
  useEffect(() => { if (forward || home.form !== 'large') setHomeOpen(false) }, [forward, home.form])
  const pressHome = useCallback(() => setHomeOpen(open => !open), [])
  // A new room opens with Home large in front (D93 1), once its pages are gone and when nobody stands forward: a Bot
  // that needs him keeps the middle, and Home is then its strip, with who waits named on it.
  const wantOpen = useRef(false)
  const [wanted, setWanted] = useState(0)
  const openHome = useCallback(() => { wantOpen.current = true; setWanted(n => n + 1) }, [])
  useEffect(() => { if (wantOpen.current && home.form === 'large') { wantOpen.current = false; if (!forward) setHomeOpen(true) } }, [wanted, home.form, forward])
  // Whether a press on the strip opens the large form now: its pages are put away and nobody came forward (D50).
  const homeOpens = placesOn && home.form === 'large' && !homeOpen && !forward
  const putPlace = useCallback((slug: string, at: Pt, state: string) => {
    const here = new Set(crewNow.current.map(b => b.slug))
    setPlacesKept(was => ({ ...Object.fromEntries(Object.entries(was).filter(([s]) => here.has(s))), [slug]: at }))
    setPlacesPut(was => ({ ...Object.fromEntries(Object.entries(was).filter(([s]) => here.has(s))), [slug]: state }))
    placedSoon.current()
  }, [crewNow, placedSoon, setPlacesKept, setPlacesPut])
  // A mark ends when its Bot's state changes. Only while the states can be known: a reload must not end it.
  useEffect(() => {
    if (!placesOn || !statesKnown) return
    const gone = Object.entries(placesPutNow.current).filter(([slug, state]) => { const b = crew.find(o => o.slug === slug); return !!b && b.state !== state })
    if (!gone.length) return
    setPlacesPut(was => Object.fromEntries(Object.entries(was).filter(([slug]) => !gone.some(([s]) => s === slug))))
    placedSoon.current()
  }, [placesOn, statesKnown, crew, placesPutNow, setPlacesPut, placedSoon])
  const placesLook = useCallback(() => bridge.current.lookYawDeg, [bridge])
  const placesGaze = useCallback(() => {
    const q = bridge.current.quat
    // How far up the head's forward points: the y of (0, 0, -1) turned by the head.
    const up = Math.max(-1, Math.min(1, -2 * (q.y * q.z - q.w * q.x)))
    return { yaw: bridge.current.lookYawDeg, pitch: (Math.asin(up) * 180) / Math.PI }
  }, [bridge])
  const frontAtStart = useMemo(() => placesFront.current, [placesFront])
  const keepFront = useCallback((front: KeptFront | null) => { placesFront.current = front; placedSoon.current() }, [placesFront, placedSoon])
  const homeAtStart = useMemo(() => placesHome.current, [placesHome])
  const keepHome = useCallback((home: Readonly<Record<string, string>>) => { if (home !== placesHome.current) { placesHome.current = home; placedSoon.current() } }, [placesHome, placedSoon])
  const placesRoom = useMemo<PlacesRoom | null>(() => (placesOn ? {
    kept: placesKept, put: placesPut, pages: pageBoxes, block: trayTaken ? 'tray' : cardKeys ? 'keys' : null, keys: cardKeys && !cardKeys.flat ? seenBox({ x: cardKeys.at[0], y: cardKeys.at[1], z: cardKeys.at[2] }, ...keysSize(cardKeys)) : undefined, homeLarge, look: placesLook, onPut: putPlace, onForward: setForward, front: frontAtStart, onFront: keepFront, boardHeld: handBusy, gaze: placesGaze, sentHome: backAsks, home: homeAtStart, onHome: keepHome,
  } : null), [placesOn, placesKept, placesPut, pageBoxes, trayTaken, cardKeys, homeLarge, placesLook, putPlace, handBusy, placesGaze, frontAtStart, keepFront, backAsks, homeAtStart, keepHome])
  const homeShown = useMemo(() => (placesOn && !homeLarge && home.form === 'large' ? { ...home, form: 'compact' as const } : home), [placesOn, homeLarge, home])
  return { placesRoom, homeShown, pressHome, openHome, homeOpens, pageBoxes, setTrayTaken, homeLarge }
}
