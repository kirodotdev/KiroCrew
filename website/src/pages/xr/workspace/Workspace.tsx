/**
 * `/xr?view=pages`: the workspace of pages. Panels in the room, and nothing else: no land, no Bots, no cards.
 *
 * What a person gains from the glasses is a bigger screen and more pages, set up as they like. This view is the
 * shell for that: each panel shows one page (`Panel.tsx`), a hand moves it by its edge and resizes it by its
 * corner, and what makes the page's pixels is a source behind a small contract (`source.ts`).
 *
 * React Three Fiber owns the canvas and the scene graph, `@react-three/xr` the session, the hands and every pointer,
 * and `@react-three/handle` each drag. On a browser with no headset it runs as a preview: the mouse or a finger is
 * the ray, a click or a tap is a pinch, and a drag that starts on a page scrolls it. The head turns by the arrow
 * keys, by a drag that starts on empty space, and by two fingers dragged anywhere, for when pages fill the view.
 * After a press on a page a real keyboard types into that page; after a finger's press on a field of a page, the
 * preview shows a text field of its own for the phone's keyboard (`typing.ts`, `PhoneField.tsx`).
 * Two fingers that move apart bring the eye nearer, until a page's text is as big on the screen as on the page;
 * together they take it back, and a button in the bar goes back to the start view (`previewLook.ts`).
 *
 * A person opens a page from the palette: in a session it is on the palm (`Palette.tsx`), in the preview it is the
 * plus in the bar (`OpenPage.tsx`); both list the same pages (`pageMenu.tsx`). The new page stands in a free place
 * in front of where the person looks. The close mark at the right end of a panel's top edge closes its page.
 *
 * The room comes back: which pages stand where is kept in the browser's own store on this device (`room.ts`), and
 * the workspace opens with it. With no room kept it opens with one chat page. `?page=/chat/<session>` asks for that
 * page of the dashboard: it opens in front when the kept room does not hold it. A page is shown live
 * (`streamSource.ts`), where the gateway has live pages turned on.
 *
 * With `?probe=1` a test can open panels and read where they are (`window.__xrPages`). That is also the only way a
 * still picture of a page or the ruled sheet gets here: none opens by itself.
 */
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import { PointerEvents, XR, noEvents, useXR } from '@react-three/xr'
import { ArrowLeft, Box, Home, MessageSquarePlus, PanelsTopLeft, RectangleGoggles } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type MutableRefObject, type PointerEvent as ReactPointerEvent, type WheelEvent as ReactWheelEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Quaternion, Vector3, type PerspectiveCamera } from 'three'
import { membersRosterQuery } from '../../../api/membersQuery'
import ErrorNotice from '../../../components/ErrorNotice'
import { Btn } from '../../../components/ui'
import { useImeGuard } from '../../../hooks/useImeGuard'
import { i18nT } from '../../../i18n/t'
import { XR_GL, readPalette } from '../palette'
import { toGl } from '../sceneParts'
import { fixtureWords } from '../fixture'
import { useXrCrew, xrNow } from '../useXrData'
import type { WorldColors } from '../world'
import { useXrSupport } from '../xrSupport'
import { AskSign } from './AskSign'
import { Boards, type BoardMarks } from './Board'
import type { FacingBox } from './deskFit'
import { dustClock } from './BoardDust'
import { ChatCard } from './ChatCard'
import { Dash3d, type DashMarks } from './Dash3d'
import { trayBlock } from './dash3d'
import { Crew, crewStroll, type CrewProbe } from './Crew'
import { wholeRead, type XrBot } from '../model'
import { boardAskOf } from './board'
import { useBoards } from './useBoards'
import { RoomNameLine } from './RoomNameLine'
import { useBotSheet } from './useBotSheet'
import { useSays, type SampleWords } from './useSays'
import type { DoneLine } from './home'
import { useComment, type SentFrom } from './useComment'
import { DeskKeys } from './DeskKeys'
import { useDash3d } from './useDash3d'
import { botPage, isBotsPage } from './crewRow'
import { deskStartPitch, seatPlaces } from './desk'
import { DeskSetup, type SetupProbe } from './DeskSetup'
import { DeskTouch, type TouchProbe } from './DeskTouch'
import { DeviceRead } from './DeviceRead'
import { deviceBook } from './deviceLines'
import { PageRects, type ScreenRect } from './ReadoutPlace'
import { SendNumbersTile } from './SendNumbersTile'
import { numbersBook, sendLine, sendToOf } from './sendNumbers'
import { shownPath } from './readoutLines'
import { SpeechTiles } from './SpeechTiles'
import { speechLine, speechProbe } from './speechProbe'
import { DomSource } from './domSource'
import { GridSource } from './gridSource'
import { PinchOpen, makePagesStore } from './hands'
import { HeadAim } from './HeadAim'
import { Home as HomeSurface, HomeMirror, type HomeProbe } from './Home'
import { useHomeNames } from './useHomeNames'
import { HomeSince, HomeSinceMirror, type HomeSinceProbe } from './HomeSince'
import { usePlacesRoom } from './usePlacesRoom'
import { useSince } from './useSince'
import { ROOM_GLIDE_MS, glideBehind, mendStanding, botsStand, roomReady, useRooms } from './useRooms'
import { DeskRoomCards, RoomsBar } from './RoomCards'
import { useHomeLines } from './useHomeLines'
import { useHome } from './useHome'
import { START_HEAD, inRoom, useKeptRoom } from './useKeptRoom'
import { useCanvasHands } from './useCanvasHands'
import { liveSourceFor, usePageOpen } from './usePageOpen'
import { useWarmRooms } from './useWarmRooms'
import { nextAhead } from './warmRooms'
import { switchPlan } from './rooms'
import { ReadoutPlate, type PlateProbe } from './ReadoutPlate'
import { Layers, type LayerShare } from './Layers'
import { StreamWatch } from './streamWatch'
import { M_PER_PX, PAGES, placeAt, ring, slotOf } from './layout'
import { OpenPage } from './OpenPage'
import { PAGE_MENU } from './pageMenu'
import { Palette, type PaletteProbe } from './Palette'
import { Panel, noteText, type PanelProbe, type Quality } from './Panel'
import { pressHeldNow } from './asks'
import { pinchRoute } from './pinchRoute'
import { PhoneField } from './PhoneField'
import { READOUT } from './readoutLines'
import { Readout } from './Readout'
import { ROOM_KEY, readRoom, readWalls, reloaded, startRoom, settleRoom, type KeptPage } from './room'
import { EmptySpace, viewAt, type NearRef, type TurnRef, type TurningRef, type ZoomNotch } from './EmptySpace'
import { newRimBook, pressCounts, pressedCuts, readRims, rimOf } from './rims'
import { LOOK, START_TURN, TURN_DRAG_PX, TURN_MS, hold, keysStay, leastNear, lensFor, lineOverCanvas, mostNear, wheelLatched, pinchNear, quietAfter, shiftAt, shiftShare, turnAt, under, type Turn, type View } from './previewLook'
import { MOST_LIVE_PAGES, type PageFocus, type PageNote, type PageSource } from './source'
import { StillSource } from './stillSource'
import { StreamSource } from './streamSource'
import { livePath } from './streamPath'
import { keyFate, keysAfter, wantsField, type KeysPage } from './typing'
import { useAskPanels } from './useAskPanels'
import { useChatCard } from './useChatCard'
import { readInSample, shapedSample } from './fixtureCrew'
import { placesWanted } from './places'
import { Desk, type DeskProbe } from './Desk'
import { useDesk, type DeskState } from './useDesk'
import { namesPlace, type Handing, type Open, type TestHook } from './testHook'
import type { TwoBefore } from './twoUp'


/**
 * What the canvas and the page outside it tell each other: where the head is and what the renderer holds, and which
 * sources are done with. `lookYawDeg` is where the person looks, to the right of straight ahead: a new page opens
 * there. The canvas draws its own tree a moment after the page changes, so only it knows when a
 * source's panel has really gone: `using` holds the sources a panel draws now, `retired` the ones to free after that.
 */
/** `frames`: the times of the room's last frames, kept only for the readout (`?fps=1`); `canvas`: the room's canvas. */
/** How long nothing of the room may have changed before a cold room is warmed ahead, and how often that is looked at (ms). */
const ROOM_REST_MS = 4000
const AHEAD_EVERY_MS = 1000

interface Bridge { head: Vector3; quat: Quaternion; lookYawDeg: number; inSession: boolean; textures: number; calls: number; using: Set<PageSource>; retired: PageSource[]; view: View; frames: number[] | null; canvas: HTMLCanvasElement | null }

/** How often a session looks for a page drawn by the tab whose paint stopped (ms). */
const HAND_OVER_EVERY_MS = 500
/** A press on the seat of a Bot whose page stands lights that page's frame this long (ms). */
const FLASH_MS = 1000

/** The preview's lens at the start: the same on every screen. It narrows as the eye comes nearer, and widens as it steps back until the first page is whole in view (`lensFor`). */
const CAMERA = { fov: LOOK.startFovDeg, near: 0.01, far: 100, position: [0, PAGES.eyeM, 0] as [number, number, number] }
const GL = { alpha: true, antialias: true } as const


/**
 * The mouse of the preview, as a pointer of the framework. In a session there is no mouse: a hand is the pointer,
 * and a mouse left lying on the window must not light what it happens to rest on.
 */
function Mouse() {
  const session = useXR(s => s.session)
  return session ? null : <PointerEvents intersectEveryFrame pointerTypePrefix="screen-" />
}


/** Inside the canvas: keeps the bridge up to date, clears to the room or to nothing, and turns the preview's head. */
function Room({ bridge, turn, turning, near, shift }: { bridge: MutableRefObject<Bridge>; turn: TurnRef; turning: TurningRef; near: NearRef; shift: MutableRefObject<number> }) {
  const gl = useThree(s => s.gl)
  const session = useXR(s => s.session)
  const origin = useXR(s => s.originReferenceSpace)
  // The room shows through an AR session; the preview has the page's own dark behind it.
  useEffect(() => { gl.setClearColor(XR_GL.room, session ? 0 : 1) }, [gl, session])
  useEffect(() => { bridge.current.canvas = gl.domElement }, [bridge, gl])
  useFrame((s, _dt, frame) => {
    // The readout's one cost in a frame: the frame's time into a ring.
    const ring = bridge.current.frames
    if (ring) { ring.push(performance.now()); if (ring.length > READOUT.ringFrames) ring.shift() }
    const presenting = s.gl.xr.isPresenting
    if (!presenting) {
      const going = turning.current
      if (going) {
        const k = (performance.now() - going.t0) / TURN_MS
        turn.current = turnAt(going.from, going.to, k)
        if (going.shift) shift.current = shiftAt(going.shift.from, going.shift.to, k)
        if (k >= 1) turning.current = null
      }
      s.camera.rotation.set((turn.current.pitchDeg * Math.PI) / 180, (-turn.current.yawDeg * Math.PI) / 180, 0, 'YXZ')
      // The preview's lens narrows as the eye comes nearer, no nearer than this canvas allows, and widens as it
      // steps back, no farther than the whole first page. A session brings its own lens.
      const lens = s.camera as PerspectiveCamera
      near.current = Math.max(leastNear(s.size.width, s.size.height), Math.min(near.current, mostNear(s.size.width, s.size.height)))
      const fovDeg = lensFor(s.size.width, s.size.height, near.current)
      if (lens.isPerspectiveCamera && lens.fov !== fovDeg) { lens.fov = fovDeg; lens.updateProjectionMatrix() }
      // The frame is shifted down, as a shift lens does it: the head stays level, so a page's sides stay upright.
      // A press is cast through the same projection, so it lands on what is drawn under it.
      // The full size is the canvas's own: the call sets the lens's shape from it. The shift is half its share of the height.
      // With the eye nearer, less of the shift is in force, and none from `LOOK.noShiftNear` on.
      const down = shift.current * shiftShare(near.current)
      const full = { w: s.size.width, h: s.size.height, y: (down / 2) * s.size.height }
      if (lens.isPerspectiveCamera && (lens.view?.enabled !== true || lens.view.fullWidth !== full.w || lens.view.fullHeight !== full.h || lens.view.offsetY !== full.y)) lens.setViewOffset(full.w, full.h, 0, full.y, full.w, full.h)
      bridge.current.view = { width: s.size.width, height: s.size.height, fovDeg, shift: down }
    } else if ((s.camera as PerspectiveCamera).view?.enabled) {
      // A session brings its own lens, and no shift.
      ;(s.camera as PerspectiveCamera).clearViewOffset()
    }
    // The head: the viewer pose itself, which is between the eyes. (three's own XR camera sits on the left eye.)
    const space = origin ?? s.gl.xr.getReferenceSpace()
    const pose = presenting && frame && space ? frame.getViewerPose(space) : null
    if (pose) {
      bridge.current.head.set(pose.transform.position.x, pose.transform.position.y, pose.transform.position.z)
      // Where the head looks, level: its own forward, turned by the pose.
      const { x, y, z, w } = pose.transform.orientation
      bridge.current.quat.set(x, y, z, w)
      bridge.current.lookYawDeg = (Math.atan2(-2 * (x * z + w * y), 1 - 2 * (x * x + y * y)) * 180) / Math.PI
    } else {
      s.camera.getWorldPosition(bridge.current.head)
      s.camera.getWorldQuaternion(bridge.current.quat)
      bridge.current.lookYawDeg = turn.current.yawDeg
    }
    bridge.current.inSession = presenting
    bridge.current.textures = s.gl.info.memory.textures
    // Read before this frame is drawn: the count is the last frame's draw calls.
    bridge.current.calls = s.gl.info.render.calls
    // A source whose panel is gone from the picture is freed. One that a panel still draws waits a frame.
    const { retired, using } = bridge.current
    for (let i = retired.length - 1; i >= 0; i -= 1) if (!using.has(retired[i])) retired.splice(i, 1)[0].dispose()
  }, -80)
  return null
}

export default function Workspace() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const probe = params.get('probe') === '1'
  // `source=dom`: a page is drawn by this tab itself where the browser can (`domSource.ts`); with no word, streamed.
  const wantsDom = params.get('source') === 'dom'
  // `desk=1`: the crew sits at seats on a desk in front of the person, and the palette's tiles lie there (`desk.ts`).
  // With no word everything is as it was.
  const deskOn = params.get('desk') === '1'
  // No row: each Bot has a desk of its own in the room, and where it is says what it does (`places.ts`). That is the
  // room with no word; `places=0` gives the row back, and so does a desk (`placesWanted`).
  const placesOn = placesWanted(params.get('places'), deskOn)
  const deskRef = useRef<DeskState | null>(null)
  const deskRead = useRef<(() => DeskProbe) | null>(null)
  const reportDesk = useCallback((read: (() => DeskProbe) | null) => { deskRead.current = read }, [])
  const setupRead = useRef<(() => SetupProbe) | null>(null)
  const reportSetup = useCallback((read: (() => SetupProbe) | null) => { setupRead.current = read }, [])
  const touchRead = useRef<(() => TouchProbe) | null>(null)
  const reportTouch = useCallback((read: (() => TouchProbe) | null) => { touchRead.current = read }, [])
  // `fps=1`: the readout of how fast the pages come (`Readout.tsx`). Nothing of it runs without the word.
  const fps = params.get('fps') === '1'
  // `layers=0` with `fps=1`: every page stays on its mesh in a session that grants layers, for comparing the two by
  // eye. Like `speech=1` it does nothing without the readout, whose `layers` line says that it applies.
  const layersOff = fps && params.get('layers') === '0'
  // `speech=1` with `fps=1`: the probe of the browser's speech recognition (`speechProbe.ts`), its tiles and its line.
  const speechOn = fps && params.get('speech') === '1'
  // `sendto=<slug>` with `fps=1`: a tile that sends the readout's numbers to that Bot's thread (`SendNumbersTile.tsx`),
  // only when the word is the slug of a Bot on his crew (D59 1; `sendToOf`, below the crew). A link never names another.
  const sendAsked = useMemo(() => (fps ? params.getAll('sendto') : []), [fps, params])
  const sendToNow = useRef('')
  // `pinch=raw` takes the framework's press as it comes, for measuring what the 70 ms rule is worth.
  const pinchOpenMs = params.get('pinch') === 'raw' ? 0 : PAGES.pinchOpenMs
  const support = useXrSupport()
  const store = useMemo(() => makePagesStore(), [])
  const [inSession, setInSession] = useState(false)
  useEffect(() => store.subscribe(s => setInSession(s.session != null)), [store])
  useEffect(() => () => { void store.getState().session?.end().catch(() => undefined) }, [store])
  const [failure, setFailure] = useState('')
  const [quality, setQuality] = useState<Quality>({ mipmaps: true, anisotropy: true })
  // With no row the room is the Bots' own, round the eye line: the start picture is not shifted down to a row under a page.
  const startShift = placesOn ? 0 : LOOK.startShift
  const bridge = useRef<Bridge>({ head: new Vector3(0, PAGES.eyeM, 0), quat: new Quaternion(), lookYawDeg: 0, inSession: false, textures: 0, calls: 0, using: new Set(), retired: [], view: { width: 1, height: 1, fovDeg: LOOK.startFovDeg, shift: startShift }, frames: fps ? [] : null, canvas: null })
  const turn = useRef<Turn>({ ...START_TURN })
  // How far the picture's frame is shifted down (`previewLook.ts`): the start picture's, until the picture turns
  // to a page by itself (`turnPicture`); the home button puts the start's back. A test may set another.
  const shift = useRef<number>(startShift)
  const near = useRef(1)
  const turning: TurningRef = useRef(null)
  // What the next change of the pages turns the picture for: a page that the plus opened, or a close.
  const lookFor = useRef<{ opened: string | null; closed: boolean }>({ opened: null, closed: false })
  const { fingers, two, taken, quiet, afterClose, hushed, host, known, hushedNow, missed, tellCanvas, inXr, hush, heldBack, downs, selects, lastKeyAt, lastWheelAt, pageDowns } = useCanvasHands({ bridge, store, turning, turn, shift, inSession })
  const readers = useRef(new Map<string, () => PanelProbe>())
  const report = useCallback((id: string, read: (() => PanelProbe) | null) => { if (read) readers.current.set(id, read); else readers.current.delete(id) }, [])
  // The panels that a hand has moved or resized: a new page never moves one of these to make room.
  const byHand = useRef(new Set<string>())
  // The pages that stand two in view, with what each goes back to (`twoUp.ts`).
  const twoBefore = useRef(new Map<string, TwoBefore>())
  // The panels that the workspace opened with from the kept record: one that the gateway has no room for stays kept.
  const fromRecord = useRef(new Set<string>())
  const placedSoon = useRef<() => void>(() => undefined)
  // A hand's carry or size of a page of a pair ends the pair (D70 2, `usePageOpen.ts`'s `twoHand`).
  const handOnPair = useRef<(id: string) => void>(() => undefined)
  const onPlaced = useCallback((id: string) => { byHand.current.add(id); handOnPair.current(id); placedSoon.current() }, [])
  const crossed = useRef(new Set<string>())
  // What each panel says in its one line now. The line is under the page, at the page's size: where that cannot be
  // read (a phone) or may be out of view (the eye near), the preview says it over the canvas as well.
  const notes = useRef(new Map<string, PageNote>())
  const [line, setLine] = useState('')
  // An ask's line is said at the canvas's upper edge: its panel's buttons stand at the lower one.
  const [lineTop, setLineTop] = useState(false)
  // What the preview says over the canvas for a little while, when a tap on a Bot can open no page. While it is
  // said, a panel's own line does not take its place.
  const said = useRef<ReturnType<typeof setTimeout> | null>(null)
  /** Say one line over the canvas for a little while. */
  const say = useCallback((words: string, top = false) => {
    setLineTop(top)
    setLine(words)
    if (said.current) clearTimeout(said.current)
    said.current = setTimeout(() => { said.current = null; setLineTop(false); const [first] = notes.current.values(); setLine(first && !store.getState().session && lineOverCanvas(bridge.current.view, near.current) ? noteText(first) : '') }, 4000)
  }, [store])
  /** One line on a Bot's own plate in the room, for as long as the line over the canvas stands (D82 3). */
  const [botNotes, setBotNotes] = useState<ReadonlyMap<string, string>>(() => new Map())
  const botNoteTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>())
  const sayAtBot = useCallback((slug: string, words: string) => {
    say(words)
    setBotNotes(was => new Map(was).set(slug, words))
    const old = botNoteTimers.current.get(slug)
    if (old) clearTimeout(old)
    botNoteTimers.current.set(slug, setTimeout(() => { botNoteTimers.current.delete(slug); setBotNotes(was => { const next = new Map(was); next.delete(slug); return next }) }, 4000))
  }, [say])
  useEffect(() => { const timers = botNoteTimers.current; return () => { for (const t of timers.values()) clearTimeout(t) } }, [])
  // The ask's panels (`useAskPanels.ts`), for the callbacks that are made before the hook is called.
  const asksRef = useRef<ReturnType<typeof useAskPanels> | null>(null)
  const onNote = useCallback((id: string, note: PageNote) => {
    if (note) notes.current.set(id, note); else notes.current.delete(id)
    if (said.current) return
    const [first] = notes.current.values()
    setLine(first && !store.getState().session && lineOverCanvas(bridge.current.view, near.current) ? noteText(first) : '')
  }, [store])
  // Typing. The page that the pointer pressed last takes the keys of a real keyboard. After a finger's press, with
  // that page's focus in a field that takes text, the phone's own field shows (`field` names the page).
  const pressed = useRef<KeysPage | null>(null)
  const focusAt = useRef(new Map<string, PageFocus>())
  const putAway = useRef(false)
  const [field, setField] = useState<string | null>(null)
  // The page's field is a password field: the phone's field is a password box then.
  const [secret, setSecret] = useState(false)
  const settleField = useCallback(() => {
    const p = pressed.current
    const focus = p ? focusAt.current.get(p.id) ?? null : null
    setField(p && wantsField(p.touch, focus, putAway.current) ? p.id : null)
    setSecret(focus?.secret === true)
  }, [])
  /** A press on a panel that shows a Bot's page is a press on that Bot for its rim (`rims.ts`); set with the crew below. */
  const rimPanelPress = useRef<(id: string) => void>(() => undefined)
  const onPress = useCallback((id: string, touch: boolean) => { pressAt.current = performance.now(); pressed.current = keysAfter(pressed.current, { press: { id, touch } }); putAway.current = false; settleField() }, [settleField])
  const onEmptyPress = useCallback(() => { pressAt.current = performance.now(); pressed.current = keysAfter(pressed.current, { away: true }); settleField() }, [settleField])
  const onFocus = useCallback((id: string, focus: PageFocus | null) => {
    if (focus) focusAt.current.set(id, focus); else focusAt.current.delete(id)
    settleField()
  }, [settleField])
  // The layer path's share: the panels put themselves in it, and read from it whether a layer shows their page.
  const layerShare = useMemo<LayerShare>(() => ({ panels: new Map(), onLayer: new Set(), writes: {} }), [])

  const place = useCallback((yawDeg: number, pitchDeg: number, distanceM: number = PAGES.distanceM) => placeAt(bridge.current.head, yawDeg, pitchDeg, distanceM), [])
  // It opens with the room that was kept on this device, and the page that `?page=` names in front when the room
  // does not hold it; with nothing kept and nothing named, with one chat page (`room.ts`).
  const [panels, setPanels] = useState<Open[]>(() => {
    let kept: KeptPage[] = []
    let walled = false
    let again = false
    try {
      mendStanding()
      const text = window.localStorage.getItem(ROOM_KEY)
      again = text !== null && reloaded()
      // A kept page that stands in the crew's place is stood once more, clear of it (`room.ts`).
      kept = settleRoom(readRoom(text))
      walled = readWalls(text)
    } catch { /* no store to read: no room is kept */ }
    return startRoom(kept, livePath(params.get('page')), again).map((p, i) => {
      if (p.byHand) byHand.current.add(p.id)
      if (p.two) twoBefore.current.set(p.id, p.two)
      if (i < kept.length) fromRecord.current.add(p.id)
      const at = placeAt(START_HEAD, p.at.yawDeg, p.at.pitchDeg, p.at.distanceM)
      // A page that was kept behind where the wall is stands in front of it while the room is on.
      return { id: p.id, source: liveSourceFor(wantsDom, p.id, p.path, p.css[0], p.css[1]), position: walled ? inRoom(START_HEAD, at) : at, css: p.css }
    })
  })
  const live = useRef(panels)
  live.current = panels
  // What the readout reads (`?fps=1`): refs only, so it renders nothing in the canvas.
  const readoutPages = useCallback(() => live.current, [])
  const readoutFrames = useCallback(() => bridge.current.frames ?? [], [])
  const readoutDraws = useCallback(() => ({ calls: bridge.current.calls, textures: bridge.current.textures }), [])
  const readoutCanvas = useCallback(() => bridge.current.canvas, [])
  // Where the pages are on the screen in the wide view, for the readout's block to lie on none (`ReadoutPlace.tsx`).
  const pageRects = useRef<ScreenRect[]>([])
  const readoutAvoid = useCallback(() => pageRects.current, [])
  // A page drawn by the tab is handed to the stream while a session runs when its paint stopped, or when it holds a
  // frame of another site that could open a browser dialog (`PageSource.streamWanted`). While the stream has no
  // picture yet it takes no pinch and lights nothing, and the readout says so, with why. One handed over for a frame
  // is drawn by the tab again when the session ends.
  const handing = useRef(new Map<string, Handing>())
  const handWhy = useRef(new Map<string, 'paint' | 'frame'>())
  const readoutNotes = useCallback(() => [...handing.current].map(([id, phase]) => {
    const frame = handWhy.current.get(id) === 'frame'
    const key = phase === 'handing' ? (frame ? 'xr.readout_handing_frame' : 'xr.readout_handing') : frame ? 'xr.readout_handed_frame' : 'xr.readout_handed'
    return i18nT(key, { name: shownPath(live.current.find(p => p.id === id)?.source.pagePath?.() ?? id) })
  }), [])
  // With `fps=1` the readout also says what the device is and does (`deviceLines.ts`), after what the room says.
  const readoutAll = useCallback(() => {
    const sent = sendToNow.current ? sendLine(numbersBook.send, sendToNow.current) : null
    return [...(sent ? [sent] : []), ...readoutNotes(), ...deviceBook.lines(), ...(speechOn ? [speechLine(speechProbe.state)] : [])]
  }, [readoutNotes, speechOn])
  const plateLines = useRef<readonly string[]>([])
  const onPlateLines = useCallback((lines: readonly string[]) => { plateLines.current = lines }, [])
  const plateCovers = useRef<PlateProbe | null>(null)
  const onPlateCovers = useCallback((plate: PlateProbe) => { plateCovers.current = plate }, [])
  useEffect(() => {
    if (!inSession) return
    // The same two maps for the whole of the component's life: kept here for the cleanup.
    const handNow = handing.current
    const whyNow = handWhy.current
    const timer = setInterval(() => {
      const stopped = live.current.filter(p => p.source.streamWanted?.() && !handing.current.has(p.id))
      for (const p of stopped) handWhy.current.set(p.id, p.source.streamWanted?.() ?? 'paint')
      for (const id of [...handing.current.keys()]) if (!live.current.some(p => p.id === id)) { handing.current.delete(id); handWhy.current.delete(id) }
      for (const p of live.current) if (handing.current.get(p.id) === 'handing' && p.source.status?.() === 'live') handing.current.set(p.id, 'handed')
      if (!stopped.length) return
      const fresh = new Map(stopped.map(p => {
        const source = new StreamSource(p.id, p.source.pagePath?.() ?? '/chat', p.css[0], p.css[1])
        asksRef.current?.wire(p.id, source)
        handing.current.set(p.id, 'handing')
        return [p.id, source] as const
      }))
      setPanels(list => list.map(p => { const source = fresh.get(p.id); return source && stopped.some(s => s.source === p.source) ? { ...p, source } : p }))
    }, HAND_OVER_EVERY_MS)
    return () => {
      clearInterval(timer)
      // The session ended: a page that went to the stream for a frame of another site is drawn by the tab again.
      const back = [...whyNow].filter(([, why]) => why === 'frame').map(([id]) => id)
      for (const id of back) { handNow.delete(id); whyNow.delete(id) }
      if (back.length) setPanels(list => list.map(p => back.includes(p.id) ? { ...p, source: liveSourceFor(wantsDom, p.id, p.source.pagePath?.() ?? '/chat', p.css[0], p.css[1]) } : p))
    }
  }, [inSession, wantsDom])
  // The pages of the rooms he left, kept open out of view (`useWarmRooms.ts`). A page that is about to be asked for
  // takes the place of a warm one: of its own path (no page is open twice), and of the oldest when the twelve are full.
  const warm = useWarmRooms({ inSession: inXr })
  const warmNow = useRef(warm)
  warmNow.current = warm
  const roomFor = useCallback((path: string) => { warm.clearPath(path); warm.makeRoom(live.current.length) }, [warm])
  // A source that is no longer shown is handed to the canvas, which frees it once its panel is gone from the picture.
  const owned = useRef<Open[]>([])
  useEffect(() => {
    // A page that is kept warm for the room he left has no panel, and is not freed (`useWarmRooms.ts`).
    const kept = new Set([...panels.map(p => p.source), ...warm.sources()])
    for (const p of owned.current) if (!kept.has(p.source)) bridge.current.retired.push(p.source)
    owned.current = panels
  }, [panels, warm])
  // The pages of a switch under way: taken from the warm set or asked for, and in no list until the switch ends.
  const switching = useRef<PageSource[]>([])
  // The page is left: the canvas goes with it, and every source is freed, also those of a switch under way.
  useEffect(() => () => { for (const s of [...owned.current.map(p => p.source), ...bridge.current.retired, ...switching.current]) s.dispose() }, [])

  const { kept, homeRef, standing, walls, setWalls, wallsNow, crewKept, setCrewKept, placesKept, setPlacesKept, placesPut, setPlacesPut, placesPutNow, placesFront, placesHome, placesNotes, sinceKept, recordNow, roomsNow, roomChanged } = useKeptRoom({ live, panels, setPanels, readers, byHand, twoBefore, fromRecord, asksRef, deskRef, placedSoon, bridge })

  const { standNew, drop, closePage, opened, openPage, calmUntil, pressAt, turnPicture, turnedFor, front, openListed, twoOn, twoToggle, twoHand } = usePageOpen({ bridge, panels, setPanels, live, readers, byHand, twoBefore, fromRecord, crossed, notes, focusAt, pressed, lookFor, deskRef, asksRef, homeRef, hush, settleField, say, inXr, room: roomFor, wantsDom, turn, shift, near, turning })
  handOnPair.current = twoHand
  // The seam of a standing pair (D69): the left page's right edge and the right page's left edge.
  const seamOf = (id: string): 'left' | 'right' | null => {
    if (!twoOn || !twoBefore.current.has(id)) return null
    const other = panels.find(q => q.id !== id && twoBefore.current.has(q.id))
    const me = panels.find(q => q.id === id)
    if (!other || !me) return null
    // By their wrapped difference: a pair formed round 180 degrees has one page at +170 and one at -170 (D80 3).
    const yaw = (v: { x: number; z: number }) => Math.atan2(v.x, -v.z)
    const apart = ((yaw(me.position) - yaw(other.position) + 3 * Math.PI) % (2 * Math.PI)) - Math.PI
    return apart < 0 ? 'right' : 'left'
  }
  // The crew, from the feed that the land uses: the real one, or with `?fixture=1` the land's own sample crew, which
  // has a Bot of every state. Its row stands under the first page (`Crew.tsx`). The pages are the real ones either way.
  const fixture = params.get('fixture') === '1'
  const sampleFeed = useXrCrew(fixture, false).crew
  // The sample crew may be filled up or have its asks chosen, for a picture (`fixtureCrew.ts`). A real crew is as it is.
  const [asksHeld, setAsksHeld] = useState(() => fixture && params.get('uplate') === '1')
  // `words=1`: the sample's Bots have words (`fixtureWords`), and the sessions he reads here are read in the sample
  // only (`readInSample`): nothing goes to the gateway. A reload starts unread again.
  // `words=net`: the sample's sessions are read from the gateway as a real crew's are, for a picture of a read that is slow or refused.
  const sampleWordsOn = fixture && params.get('words') === '1'
  const [sampleRead, setSampleRead] = useState<ReadonlySet<string>>(() => new Set())
  const sampleWords = useMemo<SampleWords | undefined>(() => (sampleWordsOn ? { messages: (slot: string) => fixtureWords(slot, params.get('failed') ? `member-${params.get('failed')}` : ''), onRead: slot => setSampleRead(was => (was.has(slot) ? was : new Set(was).add(slot))) } : fixture ? { ...(params.get('words') === 'net' ? {} : { messages: () => { throw new Error('this sample has no words') } }), onRead: slot => setSampleRead(was => (was.has(slot) ? was : new Set(was).add(slot))), onlyTapped: true } : undefined), [sampleWordsOn, fixture, params])
  const crewFeed = useMemo(() => (fixture ? readInSample(shapedSample(sampleFeed, params, asksHeld), sampleRead) : sampleFeed), [fixture, sampleFeed, params, asksHeld, sampleRead])
  // Whether the states can be known now (`Crew.tsx`): the socket is up and the lists that feed a state were read. A test may hold that back.
  const [feedHeld, setFeedHeld] = useState(false)
  const statesKnown = !feedHeld && crewFeed.feeds.live && crewFeed.feeds.asks === 'ok' && crewFeed.feeds.roster === 'ok'
  // A feed that a test holds back is a read that is not whole: no ask is new and none is gone by it.
  const askCrew = useMemo(() => (feedHeld ? { ...crewFeed, feeds: { ...crewFeed.feeds, live: false } } : crewFeed), [crewFeed, feedHeld])
  // What each Bot's rim says of its cut sessions, red or the dim mark (`rims.ts`): the book of the cuts that began
  // while he is in, and the cuts that a press on the Bot made seen. Both live only as long as the page.
  const rimBook = useRef(newRimBook())
  // What changed since his last visit (D36): Home's first line, and the Bots that are lit. It reads the Bots before
  // their rims: a cut that is new since that visit is one he has not seen, and its rim is red.
  const sinceClock = useMemo(() => xrNow(fixture), [fixture])
  const [seenCuts, setSeenCuts] = useState<ReadonlySet<string>>(() => new Set())
  // The line waits for a whole read, the list of sessions too, as every other "new or gone" rule of the room does.
  const since = useSince(askCrew.bots, statesKnown && wholeRead(askCrew.feeds), sinceClock, sinceKept, placedSoon.current, seenCuts, inSession)
  const sinceNewCuts = since.cuts
  const crew = useMemo(() => {
    readRims(rimBook.current, askCrew)
    for (const id of sinceNewCuts) rimBook.current.begun.add(id)
    return askCrew.bots.map(b => rimOf(b, rimBook.current, seenCuts))
  }, [askCrew, seenCuts, sinceNewCuts])
  const crewNow = useRef(crew)
  // A Bot put down in the row keeps that place; a Bot that left the crew is dropped from what is kept.
  const placeBot = useCallback((row: Readonly<Record<string, number>>) => {
    const here = new Set(crewNow.current.map(b => b.slug))
    setCrewKept(was => ({ ...Object.fromEntries(Object.entries(was).filter(([s]) => here.has(s))), ...row }))
    placedSoon.current()
  }, [setCrewKept])
  crewNow.current = crew
  // The Bot the numbers tile sends to: none until the crew is read, none for a word that is no Bot of his.
  const sendTo = useMemo(() => sendToOf(sendAsked, crew) ?? '', [sendAsked, crew])
  sendToNow.current = sendTo
  // With the sample crew, the names of the owner's own Bots are read too: only those have a page (`openBot`).
  // With the real crew every Bot of the row is the owner's, and nothing more is read.
  const roster = useQuery({ ...membersRosterQuery, enabled: fixture })
  const ownNames = useRef<Set<string> | null>(null)
  ownNames.current = fixture ? new Set((roster.data ?? []).map(m => m.name)) : null
  const [crewColors, setCrewColors] = useState<WorldColors | null>(null)
  useEffect(() => {
    if (!host.current) return
    const pal = readPalette(host.current)
    setCrewColors({ state: { idle: XR_GL.dim, working: toGl(pal.accent), needs: toGl(pal.warn), failed: toGl(pal.danger) } })
  }, [host])
  const still = useMemo(() => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true, [])
  const crewRead = useRef<(() => CrewProbe[]) | null>(null)
  const reportCrew = useCallback((read: (() => CrewProbe[]) | null) => { crewRead.current = read }, [])
  const probeOf = useCallback((id: string) => readers.current.get(id)?.(), [])
  // The frame light, defined with the seats' presses below: a Bot whose page stands was cut.
  const flashNow = useRef<(id: string) => void>(() => undefined)
  /** The asks on a board now, and the Bots that may hold one (drawn ones; `useBoards`, below). */
  /** When a pointer last went down anywhere on the canvas, or a pinch began: a line's wait for its board ends there. */
  const anyDownAt = useRef(-Infinity)
  /** For the probe: the last pinch on a Bot and what it did (`useBotSheet`). */
  const botSheetNow = useRef<(() => unknown) | null>(null)
  const boardAsksNow = useRef<ReadonlySet<string>>(new Set())
  const boardCrewNow = useRef<readonly XrBot[]>([])
  // An approval that a board will hold counts as held from the read on, before the board is drawn: its panel must not
  // come up in between. Only that ask is held back: another ask of the same Bot (a question) comes up as before.
  // With no row no ask's page comes up by itself, whatever its kind: the Bot comes to him, and its page opens when he asks for it.
  const boardAskers = useCallback(() => new Set([...boardAsksNow.current, ...boardCrewNow.current.flatMap(b => { const n = boardAskOf(b); return n ? [n.id] : [] }), ...(placesOn ? crewNow.current.flatMap(b => b.needs.map(n => n.id)) : [])]), [placesOn])
  // The Bots whose board stands, or will from this read on: every other ask of theirs waits behind it (D24).
  const boardHoldersNow = useRef<ReadonlySet<string>>(new Set())
  // An approval that a board already answered and that is still listed holds nothing, as in `boardsNow`.
  const boardsAnsweredNow = useRef<() => ReadonlySet<string>>(() => new Set())
  const boardBots = useCallback(() => {
    const answered = boardsAnsweredNow.current()
    return new Set([...boardHoldersNow.current, ...boardCrewNow.current.flatMap(b => (boardAskOf({ needs: b.needs.filter(n => !answered.has(n.id)) }) ? [b.slug] : []))])
  }, [])
  // The Bots whose board he handed back himself: their questions come up past it (D27).
  const handedBackNow = useRef<ReadonlySet<string>>(new Set())
  const handedBack = useCallback(() => handedBackNow.current, [])
  // A pointer whose lift was never heard counts as down for twenty seconds, and no longer.
  const handsBusy = () => [...downs.current.values(), ...pageDowns.current.values()].some(at => performance.now() - at < 20_000) || two.current !== null || selects.current > 0 || hushedNow.current || performance.now() < quiet.current || turning.current !== null
  const asks = useAskPanels(askCrew, () => crewNow.current, {
    panels: () => live.current,
    probe: probeOf,
    crewNow: () => crewRead.current?.() ?? [],
    head: () => bridge.current.head,
    quat: () => bridge.current.quat,
    view: () => bridge.current.view,
    turn, near, shift, inXr,
    walls: () => wallsNow.current,
    deskPoints: () => deskRef.current?.points(),
    busy: handsBusy,
    lastKeyAt, lastWheelAt,
    fieldPx: () => {
      const type = document.querySelector('[data-testid="xr-type"]')
      const canvas = document.querySelector('[data-testid="xr-canvas"]')
      return type && canvas ? Math.max(0, canvas.getBoundingClientRect().bottom - type.getBoundingClientRect().top) : 0
    },
    liveSource: (id, path, w, h) => { roomFor(path); return liveSourceFor(wantsDom, id, path, w, h) },
    add: panel => setPanels(list => [...list, panel]),
    move: (id, to, turned) => setPanels(list => list.map(p => (p.id === id ? { ...p, position: to, turned } : p))),
    drop: id => drop(p => p.id === id),
    hush: () => hush(true),
    cameUp: id => { pressed.current = keysAfter(pressed.current, { cameUp: id }) },
    say,
    roomChanged: () => placedSoon.current(),
    placed: onPlaced,
    flash: id => flashNow.current(id),
    boards: boardAskers,
    boardBots,
    handedBack,
    noPages: () => placesOn,
  })
  asksRef.current = asks
  // The Home surface: its numbers, and the switch that puts the real pages away (`useHome.ts`). The numbers count the
  // Bots as the rims draw them, so "failed" is the Bots that the row shows red.
  const home = useHome({
    panels: () => live.current,
    isAsk: id => asksRef.current?.isAsk(id) ?? false,
    settle: () => {
      const now = new Map(live.current.map(p => [p.id, readers.current.get(p.id)?.()]))
      setPanels(list => list.map(p => { const at = now.get(p.id); return at ? { ...p, position: new Vector3(...at.position), css: at.css } : p }))
    },
    away: onEmptyPress,
    hush: () => hush(true),
    roomChanged: () => placedSoon.current(),
    now: xrNow(fixture),
    asked: livePath(params.get('page')) !== null,
    showWord: params.get('pages') === 'show',
  }, panels.map(p => p.id), crew, askCrew.needs, statesKnown)
  homeRef.current = home
  const cardMicAway = useRef<((away: boolean) => void) | null>(null)
  // During a switch: the pages of the room he goes to, for the crew (`usePlacesRoom`, `ahead`).
  const [roomAhead, setRoomAhead] = useState<{ position: Vector3; css: [number, number] }[] | null>(null)
  const rooms = useRooms({
    record: recordNow, wrote: text => { kept.current = text }, open: () => standing().map(p => ({ id: p.id, path: p.page.path })),
    place: async (go, stay, come, away, ahead, { from, to }) => {
      const at = (k: KeptPage) => { const v = placeAt(START_HEAD, k.at.yawDeg, k.at.pitchDeg, k.at.distanceM); return wallsNow.current ? inRoom(START_HEAD, v) : v }
      // The pages of the room he goes to that are warm come as they are, whole, and are told that he sees them again;
      // the others are asked for, and the oldest warm rooms close when the twelve would not hold them.
      const warmed = warm.take(to, come)
      // The taken pages are open at the gateway, and in no list now: they count.
      warm.makeRoom(live.current.length + warmed.filter(Boolean).length, warmed.filter(w => !w).length)
      const made = come.map((k, i) => {
        const w = warmed[i]
        if (w) { if (w.css.join() !== k.css.join()) w.source.resize(...k.css); if (!away) { if (w.source.takenBack) w.source.takenBack(); else w.source.visible?.(true) } return { id: w.id, source: w.source, position: at(k), css: k.css, byHand: k.byHand, two: k.two, warm: true } }
        opened.current += 1
        const id = `room-${opened.current}`
        return { id, source: liveSourceFor(wantsDom, id, k.path, k.css[0], k.css[1]), position: at(k), css: k.css, byHand: k.byHand, two: k.two, warm: false }
      })
      switching.current = made.map(m => m.source)
      // The crew is told of the new room now: its pages where they will stand (none when it keeps them put away), and
      // where it keeps its Bots. They change spots while the old room stands.
      setRoomAhead(away ? [] : [...stay.map(s => ({ position: at(s.to), css: s.to.css })), ...made.map(m => ({ position: m.position, css: m.css }))])
      ahead()
      // The room he leaves stands until the new pages have their picture and the Bots stand at their places (or a
      // while has passed): no blank page shows, and no frame shows the room without its Bots.
      // A warm page is whole already: only the pages that were asked for are waited for.
      await Promise.all([roomReady(made.filter(m => !m.warm).map(m => m.source)), botsStand(() => (crewRead.current?.() ?? []).map(b => b.grow ?? 1))])
      setRoomAhead(null)
      // The pages that go stay open for the room he leaves when each was whole and they fit; else they close as before.
      const leaving = standing().filter(p => go.includes(p.id)).flatMap(p => { const open = live.current.find(o => o.id === p.id); return open ? [{ id: p.id, path: p.page.path, css: open.css, source: open.source }] : [] })
      warm.leave(from, leaving.length === go.length ? leaving : [], stay.length + made.length)
      leftAt.current.set(from, Date.now())
      drop(p => go.includes(p.id))
      // A new page that the gateway had no room for while the old ones stood is asked for again now.
      for (const m of made) if (!m.warm) m.source.askAgain?.()
      for (const s of stay) { if (s.to.byHand) byHand.current.add(s.id); else byHand.current.delete(s.id) }
      for (const m of made) { if (m.byHand) byHand.current.add(m.id); fromRecord.current.add(m.id) }
      // The room he goes to brings its own pair, or none (`twoUp.ts`).
      twoBefore.current.clear()
      for (const s of stay) if (s.to.two) twoBefore.current.set(s.id, s.to.two)
      for (const m of made) if (m.two) twoBefore.current.set(m.id, m.two)
      const behind = glideBehind([...stay.map(s => ({ id: s.id, at: at(s.to) })), ...made.map(m => ({ id: m.id, at: m.position }))], bridge.current.head, bridge.current.lookYawDeg)
      setPanels(list => [...list.map(p => { const s = stay.find(x => x.id === p.id); if (!s) return p; if (s.to.css.join() !== p.css.join()) p.source.resize(...s.to.css); return { ...p, position: at(s.to), css: s.to.css, glide: true, behindM: behind(p.id) } }), ...made.map(({ byHand: _hand, warm: _warm, two: _two, ...m }) => m)])
      switching.current = []
      setTimeout(() => setPanels(list => (list.some(p => p.glide) ? list.map(p => (p.glide ? { ...p, glide: false } : p)) : list)), ROOM_GLIDE_MS + 200)
    },
    setCrew: setCrewKept, setPlaces: places => { setPlacesKept(places); setPlacesPut({}) }, setAway: on => homeRef.current?.setAway(on),
    say, micAway: away => cardMicAway.current?.(away),
  })
  roomsNow.current = rooms
  // Warm ahead (`useWarmRooms.ts`): once the room he stands in is at rest, the rooms that are cold are opened out of
  // his sight, one at a time, the ones he left most recently first. Never while a switch is under way, his hands are
  // busy, a board or a sheet is in them, or a card of his is on its way; never for pages that the tab draws itself.
  // `?ahead=0` switches it off (a row that reads which pages a switch itself keeps).
  const leftAt = useRef(new Map<string, number>())
  /**
   * A warm room must hold every page that a switch to it would ask for: one that stood here and closed since leaves it
   * not whole, and its dot goes out (`useWarmRooms.ts`, `prune`).
   */
  const pruneWarm = useCallback(() => {
    const r = roomsNow.current
    if (!r || r.moving()) return
    const open = standing().map(p => ({ id: p.id, path: p.page.path }))
    warmNow.current.prune(room => {
      const k = r.rooms.rooms.find(x => x.id === room)
      return k ? switchPlan(open, readRoom(k.layout)).come : null
    })
  }, [standing, roomsNow])
  // Also when a warm room closes (a refusal, want of room): a room that needed one of its pages is cold then.
  useEffect(() => { pruneWarm() }, [panels, pruneWarm, warm.warm])
  const restSince = useRef(performance.now())
  useEffect(() => { restSince.current = performance.now() }, [panels, rooms.rooms.now])
  const aheadHeld = useRef<() => boolean>(() => false)
  const handBusyNow = useRef(false)
  const aheadWhy = useRef<string | null>(null)
  useEffect(() => {
    if (wantsDom || params.get('ahead') === '0') return
    const timer = setInterval(() => {
      const w = warmNow.current
      const r = roomsNow.current
      if (!r) return
      w.tend(live.current.length)
      // Why nothing is warmed ahead now (for a row to read): one is under way, a switch, his hands, the room not at rest.
      const why = w.warming ? `warming ${w.warming}` : r.moving() ? 'a switch' : aheadHeld.current() ? 'his hands' : performance.now() - restSince.current < ROOM_REST_MS ? 'the room changed' : !live.current.every(p => (p.source.status?.() ?? 'live') === 'live') ? 'a page loads' : null
      aheadWhy.current = why
      if (why) return
      const open = standing().map(p => ({ id: p.id, path: p.page.path }))
      // A page that an ask's panel shows is open already: a room that would ask for its path again waits for the ask to go.
      const asked = new Set(live.current.filter(p => asksRef.current?.isAsk(p.id)).map(p => p.source.pagePath?.() ?? ''))
      const cold = r.rooms.rooms.filter(k => k.id !== r.rooms.now && w.mayAhead(k.id))
        .map(k => ({ id: k.id, come: switchPlan(open, readRoom(k.layout)).come }))
        .filter(k => !k.come.some(c => asked.has(c.path)))
        .sort((a, b) => (leftAt.current.get(b.id) ?? 0) - (leftAt.current.get(a.id) ?? 0))
      const next = nextAhead(cold.map(c => ({ id: c.id, come: c.come.length })), live.current.length + w.count())
      const come = cold.find(c => c.id === next?.id)?.come
      if (!next || !come) { aheadWhy.current = cold.length ? 'no cold room fits' : 'no cold room'; return }
      const pages = come.map(k => { opened.current += 1; const id = `ahead-${opened.current}`; return { id, path: k.path, css: k.css, source: liveSourceFor(false, id, k.path, k.css[0], k.css[1]) } })
      if (!w.warmAhead(next.id, pages)) for (const p of pages) p.source.dispose()
    }, AHEAD_EVERY_MS)
    return () => clearInterval(timer)
  }, [wantsDom, params, live, standing, opened, roomsNow])
  // A room that is no longer in the list (removed, here or in another tab) keeps no page open.
  useEffect(() => { warm.only(rooms.rooms.rooms.map(k => k.id)) }, [warm, rooms.rooms])
  const roomCards = useMemo(() => ({ rooms: rooms.rooms, onGo: rooms.go, onAdd: () => rooms.addEmpty(bridge.current.lookYawDeg), onAsk: (id: string) => rooms.ask(id, false, bridge.current.lookYawDeg), onCopy: rooms.copy, onTold: rooms.told, onHold: rooms.hold, onLetGo: rooms.letGo, naming: rooms.naming, gone: rooms.gone?.room ?? null, onBack: rooms.back, said: rooms.said, warm: warm.warm, onRest: warm.rest }), [rooms, warm.warm, warm.rest])
  const homeRead = useRef<(() => HomeProbe) | null>(null)
  const reportHome = useCallback((read: (() => HomeProbe) | null) => { homeRead.current = read }, [])
  const sinceRead = useRef<(() => unknown) | null>(null)
  const sinceLineRead = useRef<(() => HomeSinceProbe | null) | null>(null)
  const reportSince = useCallback((read: (() => HomeSinceProbe | null) | null) => { sinceLineRead.current = read }, [])
  /** Home's band as it stands now, the strip (or large Home) and the since line: no desk line reaches into it (D39). Read every frame by the boards, so Home and the line report always. */
  const homeBand = useCallback((): FacingBox[] => [homeRead.current?.(), sinceLineRead.current?.()].flatMap(x => (x ? [{ center: x.center, size: x.size }] : [])), [])
  // A press that the gates hold back is no press on the panel's Bot either: it counts for the rim only when it goes
  // to the page or keeps the panel.
  /**
   * Whether a press on a panel's page would be dropped now, read each frame for the dot's light (D53 2): the same
   * words `mayPress` reads, and the quiet time after a close, with nothing written. The page itself stays at full light.
   */
  const pressHeld = useCallback((id: string) => pressHeldNow(id, performance.now(), {
    handing: handing.current, calmUntil: calmUntil.current,
    quiet: now => (inXr() ? pinchRoute.quiet(now) : quiet.current > now),
    asks: asksRef.current ? asksRef.current.pressHeld : null,
  }), [calmUntil, inXr, quiet])
  const mayPress = useCallback((id: string) => {
    if (handing.current.get(id) === 'handing' || (calmUntil.current.get(id) ?? 0) > performance.now()) return false
    const fate = asksRef.current?.pressFate(id) ?? 'send'
    if (pressCounts(fate)) rimPanelPress.current(id)
    return fate === 'send'
  }, [calmUntil])
  // The person changed the lens himself: an ask that set it back does not put it anywhere after that.
  const onLens = useCallback(() => asksRef.current?.lensChanged(), [])
  const onWheelOn = useCallback((id: string) => asksRef.current?.wheelOn(id), [])
  // A page that the browser hands input to itself (`domSource.ts`): its press, notch and key make the person busy as
  // those that the panel passes on do, and a key or a notch on an ask's panel makes it his page.
  useEffect(() => {
    for (const p of panels) p.source.listen?.(input => {
      const now = performance.now()
      if (input === 'down') { pageDowns.current.set(p.id, now); pressAt.current = now; rimPanelPress.current(p.id) }
      else if (input === 'up') pageDowns.current.delete(p.id)
      else if (input === 'wheel') { lastWheelAt.current = now; asksRef.current?.wheelOn(p.id) }
      else { lastKeyAt.current = now; asksRef.current?.typedInto(p.id) }
    })
    for (const id of [...pageDowns.current.keys()]) if (!panels.some(p => p.id === id)) pageDowns.current.delete(id)
  }, [panels, lastKeyAt, lastWheelAt, pageDowns, pressAt])
  // A notch on a page: the person scrolls, whether or not the page takes this one.
  const zoomAt = useRef<ZoomNotch | null>(null)
  const zoomer = useRef<((deltaY: number, x: number, y: number) => void) | null>(null)
  const zoomLatched = useCallback((x: number, y: number, deltaY?: number) => {
    if (!wheelLatched(zoomAt.current, x, y, performance.now()) || !zoomer.current) return false
    if (deltaY !== undefined) zoomer.current(deltaY, x, y)
    return true
  }, [])
  const mayWheel = useCallback((id: string) => { lastWheelAt.current = performance.now(); return asksRef.current?.mayWheel(id) ?? true }, [lastWheelAt])
  // The pinches of a session: one that is down makes the person busy.
  useEffect(() => {
    const session = store.getState().session
    if (!inSession || !session) return
    const down = () => { selects.current += 1; pressAt.current = performance.now(); anyDownAt.current = pressAt.current }
    const up = () => { selects.current = Math.max(0, selects.current - 1) }
    session.addEventListener('selectstart', down)
    session.addEventListener('selectend', up)
    return () => { session.removeEventListener('selectstart', down); session.removeEventListener('selectend', up); selects.current = 0 }
  }, [inSession, store, selects, pressAt])
  /** The panel that was last opened for each Bot's address, and when. */
  const botOpened = useRef(new Map<string, { id: string; at: number }>())
  useEffect(() => () => { if (said.current) clearTimeout(said.current) }, [])
  /**
   * A tap on a Bot: the page to read that Bot on opens on a panel, as the plus opens one, and the picture turns to
   * it. A panel that shows that page already is turned to instead. With as many pages open as the gateway holds,
   * none opens, and the line says why. Nothing is sent to any page.
   */
  /** The panel whose frame lights now, for a press on its Bot's seat (`desk=1`): one at a time, for `FLASH_MS`. */
  const [flashed, setFlashed] = useState<string | null>(null)
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const flashPanel = useCallback((id: string) => {
    if (flashTimer.current) clearTimeout(flashTimer.current)
    setFlashed(id)
    flashTimer.current = setTimeout(() => { flashTimer.current = null; setFlashed(null) }, FLASH_MS)
  }, [])
  flashNow.current = flashPanel
  useEffect(() => () => { if (flashTimer.current) clearTimeout(flashTimer.current) }, [])
  /** A press on a Bot: every cut of it that was read is seen, and its rim shows the dim mark for them. */
  const pressRim = useCallback((slug: string) => {
    const ids = pressedCuts(crewNow.current.find(b => b.slug === slug) ?? { cuts: [] })
    setSeenCuts(seen => (ids.every(id => seen.has(id)) ? seen : new Set([...seen, ...ids])))
  }, [])
  rimPanelPress.current = id => {
    const panel = live.current.find(p => p.id === id)
    if (!panel) return
    const path = panel.source.pagePath?.() ?? null
    const bot = crewNow.current.find(b => isBotsPage(path, botOpened.current.get(botPage(b) ?? '')?.id === id, b, crewNow.current))
    // In a session a press inside a Bot's page marks no cut seen (D98 7, M69): only its failed sheet in his hand does.
    if (bot && !inXr()) pressRim(bot.slug)
  }
  /** The first part of a press on a Bot: its rim, the desk, and its ask's panel. True when the ask's panel took it. */
  const pressBot = useCallback((slug: string, rim = true): boolean => {
    // `rim` false: any pinch on a Bot in a session, whatever it brings (an ask's board, a working sheet, a page).
    // Its cut is seen when its failed sheet is in his hand (D92 1, D98 7).
    if (rim) pressRim(slug)
    // A press on a seat is a press on the desk: a kept desk that was a guess is taken as right.
    deskRef.current?.confirm()
    // A Bot that asks on a card: its ask's panel comes up straight ahead, or the picture turns to it where a part of it shows.
    const up = asksRef.current?.tapBot(slug) ?? false
    if (up === true) return true
    if (typeof up === 'string') {
      const standing = live.current.find(p => p.id === up)
      const now = readers.current.get(up)?.()
      if (standing && deskRef.current) flashPanel(up)
      if (standing && !inXr()) turnPicture({ heightM: now ? now.size[1] : standing.css[1] * M_PER_PX, ...slotOf(bridge.current.head, now ? new Vector3(...now.position) : standing.position) }, false)
      return true
    }
    return false
  }, [flashPanel, inXr, pressRim, turnPicture])
  const openBot = useCallback((slug: string) => {
    const bot = crewNow.current.find(b => b.slug === slug)
    // In a session a page that opens marks no cut seen (D98 7): a failure is named on Home until its own sheet was in his hand.
    if (!bot || pressBot(slug, !inXr())) return
    // The gateway opens the page of one of the owner's Bots and of no other name. A Bot of the sample crew has a
    // page only when the owner has a Bot of that name; a name that no address can carry has none. The Bot has
    // answered by itself; the line says why no page comes.
    const path = ownNames.current && !ownNames.current.has(bot.name) ? null : botPage(bot)
    if (!path) { sayAtBot(slug, i18nT('xr.bot_no_page', { name: bot.label })); return }
    // The page of this Bot, and of no other: a panel whose tab moved on to another Bot is not it. A panel that was
    // opened for this address a moment ago counts too, before it is drawn: two quick taps open one panel.
    const fresh = botOpened.current.get(path)
    if (fresh && performance.now() - fresh.at < 2000 && !live.current.some(p => p.id === fresh.id)) return
    // Its page stands. With no desk=1, a panel that shows the Bot's page now, as the eighth found it. With the word,
    // the rule of a Bot's page for a second press (`isBotsPage`): that page or the chat of one of its sessions now, or a
    // panel opened with it that has not gone to another Bot.
    const openedId = botOpened.current.get(path)?.id
    const shown = deskRef.current
      ? live.current.find(p => isBotsPage(p.source.pagePath?.() ?? null, openedId === p.id, bot, crewNow.current))
      : live.current.find(p => p.source.pagePath?.() === path)
    if (shown) {
      homeRef.current?.reveal(shown.id)
      if (deskRef.current) flashPanel(shown.id)
      if (inXr()) return
      const now = readers.current.get(shown.id)?.()
      turnPicture({ heightM: now ? now.size[1] : shown.css[1] * M_PER_PX, ...slotOf(bridge.current.head, now ? new Vector3(...now.position) : shown.position) }, false)
      return
    }
    if (live.current.length >= MOST_LIVE_PAGES) { say(noteText('full')); return }
    const before = { turn: { ...turn.current }, shift: shift.current }
    const opened = openPage(path)
    if (!opened) return
    botOpened.current.set(path, { id: opened.id, at: performance.now() })
    // The gateway has the last word on a Bot's page: it opens one for an owner's Bot only, and none when it cannot
    // read who they are. A page that it refuses leaves no panel behind: the picture is as before, and the line says why.
    const stop = opened.source.subscribe(() => {
      const status = opened.source.status?.() ?? 'live'
      if (status === 'loading') return
      stop()
      if (!(opened.source.refusedOpen?.() ?? false)) return
      drop(p => p.id === opened.id)
      // The picture goes back only if it still looks where the turn to this panel left it, or is on its way
      // there. A person who has turned it since keeps what they turned to.
      const made = turnedFor.current?.id === opened.id ? turnedFor.current.to : null
      const same = (a: Turn) => made !== null && Math.abs(a.yawDeg - made.yawDeg) < 1e-6 && Math.abs(a.pitchDeg - made.pitchDeg) < 1e-6
      if (!inXr() && same(turning.current ? turning.current.to : turn.current)) { turning.current = null; turn.current = before.turn; shift.current = before.shift }
      sayAtBot(slug, i18nT('xr.bot_no_page', { name: bot.label }))
    })
  }, [drop, flashPanel, inXr, openPage, pressBot, say, sayAtBot, turnPicture, homeRef, turnedFor])
  // A Bot's words are a sheet on its clipboard (D19, `useSays.ts`): a tap on a Bot holds its words up, its last ones
  // when it has nothing new (T3), and opens no page. A Bot that holds a board up already keeps it.
  const says = useSays(crew, true, since.tapped, sampleWords)
  const saysNow = useRef(says.says)
  saysNow.current = says.says
  /** For the probe: what each Bot's clipboard says of its words, and whether its own thread is unread. */
  const saysRead = () => Object.fromEntries(crewNow.current.map(b => { const w = saysNow.current.get(b.slug); return [b.slug, { raise: !!w?.raise, state: w?.state ?? '', sentence: w?.sentence ?? '', sheets: w?.sheets.length ?? 0, ownUnread: b.ownUnread, slot: w?.slot ?? '', version: w?.version ?? '', done: w?.done ?? null, rest: b.rest.map(r => ({ slot: r.slot, unread: r.unread, ts: r.ts })) }] }))
  const boardViewsNow = useRef<readonly { bot: string }[]>([])
  const saysTap = says.tap
  const tapBot = useCallback((slug: string) => {
    const bot = crewNow.current.find(b => b.slug === slug)
    if (!bot || pressBot(slug, !inXr())) return
    if (!boardViewsNow.current.some(b => b.bot === slug)) saysTap(slug)
  }, [pressBot, saysTap, inXr])
  /** A line of Home's "done today": that sheet comes up at its Bot again, as a tap holds words up (D35). */
  const pressDone = useCallback((line: DoneLine) => { if (!boardViewsNow.current.some(b => b.bot === line.bot)) saysTap(line.bot, line.slot) }, [saysTap])
  // The 3D dashboards (`dash3d.ts`): a Bot whose own Dashboard holds a layout has one at its feet.
  const dash = useDash3d(crew, fixture, probe)
  const dashMarksRead = useRef<(() => Record<string, DashMarks>) | null>(null)
  const reportDash = useCallback((read: (() => Record<string, DashMarks>) | null) => { dashMarksRead.current = read }, [])
  // The palette on the palm, as a test reads it.
  const paletteRead = useRef<(() => PaletteProbe) | null>(null)
  const reportPalette = useCallback((read: (() => PaletteProbe) | null) => { paletteRead.current = read }, [])
  // The dashboard's own icons for the palette's tiles, drawn once, out of sight (`palettePicture.ts`).
  const menuIcons = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!probe) return
    const hooked = window as unknown as { __xrPages?: TestHook }
    hooked.__xrPages = {
      open: spec => {
        const w = spec.cssWidth ?? PAGES.cssWidth
        const h = spec.cssHeight ?? PAGES.cssHeight
        const page = livePath(spec.live ?? null)
        const source: PageSource = page ? (spec.copies === 'always' ? new StreamSource(spec.id, page, w, h, true) : liveSourceFor(wantsDom, spec.id, page, w, h)) : spec.stills?.length ? new StillSource(spec.id, spec.stills, w) : new GridSource(spec.id, w, h)
        // The sample page that says a line (D80 2): only with the sample data, never over a live page's own word.
        if (fixture && spec.note && !page) { const said = spec.note; source.note = () => said }
        byHand.current.delete(spec.id)
        fromRecord.current.delete(spec.id)
        crossed.current.delete(spec.id)
        if (!namesPlace(spec)) { setPanels(list => standNew(list.filter(p => p.id !== spec.id), spec.id, source, [w, h])); return }
        const position = place(spec.yawDeg ?? 0, spec.pitchDeg ?? PAGES.lowerDeg, spec.distanceM)
        setPanels(list => [...list.filter(p => p.id !== spec.id), { id: spec.id, source, position, css: [w, h] }])
      },
      text: (id, text) => live.current.find(p => p.id === id)?.source.text?.(text),
      key: (id, key) => live.current.find(p => p.id === id)?.source.key?.({ type: 'down', key }),
      close: id => drop(p => p.id === id),
      clear: () => drop(() => true),
      arrange: () => setPanels(list => { const at = ring(list.length); return list.map((p, i) => ({ ...p, position: place(at[i].yawDeg, at[i].pitchDeg, at[i].distanceM) })) }),
      quality: q => setQuality(now => ({ ...now, ...q })),
      asksCome: (on = true) => setAsksHeld(!on),
      stroll: (all, everyMs) => { crewStroll.all = all; if (everyMs && everyMs >= 200) crewStroll.everyMs = everyMs },
      stale: on => setFeedHeld(on),
      paintless: id => { const s = live.current.find(p => p.id === id)?.source; if (s instanceof DomSource) s.paintless = true },
      boardStale: id => boardsStale.current(id),
      dustAt: at => { dustClock.pinnedS = at },
      cardClose: () => chatCardNow.current.close(),
      page: path => { openPage(path) },
      room: (act, id = '', name = '') => { const r = roomsNow.current; if (r) { if (act === 'go') r.go(id); else if (act === 'add') r.add(); else if (act === 'new') r.addEmpty(bridge.current.lookYawDeg); else if (act === 'copy') r.copy(); else if (act === 'ask') r.ask(id, false, bridge.current.lookYawDeg); else if (act === 'rename') r.rename(id, name); else if (act === 'remove') r.remove(id); else r.back() } },
      look: to => {
        const view = bridge.current.view
        turning.current = null
        turn.current = { yawDeg: to.yawDeg ?? turn.current.yawDeg, pitchDeg: to.pitchDeg ?? turn.current.pitchDeg }
        if (to.lens !== undefined) { near.current = to.lens === 'page' ? leastNear(view.width, view.height) : to.lens === 'start' ? 1 : to.lens; asksRef.current?.lensChanged() }
        if (to.shift !== undefined) shift.current = to.shift
      },
      read: () => ({
        panels: [...readers.current.values()].map(read => read()),
        sources: Object.fromEntries(live.current.map(p => [p.id, { seen: (p.source as { seen?: unknown }).seen ?? null, shown: (p.source as { shown?: unknown }).shown ?? null, path: p.source.pagePath?.() ?? null, status: p.source.status?.() ?? null, note: p.source.note?.() ?? '' }])),
        head: [bridge.current.head.x, bridge.current.head.y, bridge.current.head.z], inSession: bridge.current.inSession, textures: bridge.current.textures, calls: bridge.current.calls,
        placed: [...byHand.current], crossed: [...crossed.current],
        layers: { onLayer: [...layerShare.onLayer], writes: { ...layerShare.writes } },
        turn: { ...turn.current }, turning: turning.current !== null, keys: pressed.current?.id ?? null, walls: wallsNow.current, view: { ...bridge.current.view }, near: near.current, least: leastNear(bridge.current.view.width, bridge.current.view.height),
        home: homeRead.current?.() ?? null, rooms: roomsNow.current ? { ...roomsNow.current.rooms, sleeping: warmNow.current.sleeping, gone: roomsNow.current.gone, naming: roomsNow.current.naming, nameAsk: roomsNow.current.nameAsk, said: roomsNow.current.said, warm: warmNow.current.warm, warmPages: warmNow.current.count(), warming: warmNow.current.warming, aheadWaits: aheadWhy.current, left: warmNow.current.left() } : null, since: sinceRead.current?.() ?? null, shownPanels: live.current.filter(p => !homeRef.current?.hides(p.id)).map(p => p.id),
        palette: paletteRead.current?.() ?? null, crew: crewRead.current?.() ?? [], asks: asksRef.current?.read() ?? null, desk: deskRead.current?.() ?? null, card: chatCardNow.current.read(), setup: setupRead.current?.() ?? null, touch: touchRead.current?.() ?? null, boards: boardsRead.current(), boardMarks: boardMarksRead.current?.() ?? {}, dash3d: dashMarksRead.current?.() ?? {}, says: saysRead(), botSheet: botSheetNow.current?.() ?? null, note: commentNow.current?.read() ?? null,
        pinch: bridge.current.inSession ? pinchRoute.probe : null, plate: plateLines.current, plateCovers: plateCovers.current, numbers: sendTo ? { to: sendTo, ...numbersBook.send } : null, handing: Object.fromEntries(handing.current),
      }),
    }
    return () => { delete hooked.__xrPages }
  }, [probe, place, standNew, layerShare, drop, wantsDom, homeRef, roomsNow, wallsNow, openPage, sendTo, fixture])

  const enter = useCallback(async () => {
    if (support === 'none') return
    try {
      await (support === 'ar' ? store.enterAR() : store.enterVR())
    } catch (e) {
      setFailure(e instanceof Error ? e.message : String(e))
    }
  }, [store, support])

  /** The source of the page that takes the keyboard now, when it takes text at all. */
  const typingInto = (id: string | undefined) => { const s = live.current.find(p => p.id === id)?.source; return s?.text && s.key ? s : null }
  /**
   * Where text and keys leave for a page. In the flat preview they go only to a page that the person can see
   * (`keysStay`). The picture can be turned away from the page that has the keys by the home button, by two
   * fingers, by a window that changes its shape: the keys are then let go, as after a press on empty space, and
   * nothing is sent. In a session the person turns the head, and there is no such rule.
   */
  const keysFor = (id: string | undefined) => {
    const source = typingInto(id)
    const open = live.current.find(p => p.id === id)
    if (!source || !open || inXr()) return source
    const now = readers.current.get(open.id)?.()
    const at = slotOf(bridge.current.head, now ? new Vector3(...now.position) : open.position)
    const size: [number, number] = now ? now.size : [open.css[0] * M_PER_PX, open.css[1] * M_PER_PX]
    if (keysStay(turn.current, bridge.current.view, at, size)) return source
    onEmptyPress()
    return null
  }
  // A page's focus can move by itself (a message is sent, a card opens): while the phone's field is open, ask.
  useEffect(() => {
    if (!field) return
    // The same net as for a key: a field whose page went out of the picture goes down by itself.
    const timer = setInterval(() => keysFor(field)?.refocus?.(), 1500)
    return () => clearInterval(timer)
    // `keysFor` reads refs alone: the one of the render that showed the field is as good as any.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [field])

  // A key that an input method is still working on is the browser's, by the dashboard's own guard.
  const ime = useImeGuard()
  // The preview has no head to turn: the arrow keys turn it. After a press on a page the keyboard is that page's.
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    // While his note's line is open a real keyboard writes the note; while the card's keyboard shows, on the card.
    if (!e.ctrlKey && !e.metaKey && !e.altKey && !ime.isComposing(e) && (commentNow.current?.typed(e.key) || chatCard.typed(e.key))) { e.preventDefault(); return }
    const source = keysFor(pressed.current?.id)
    const p = pressed.current
    const fate = keyFate({ key: e.key, ctrlKey: e.ctrlKey, metaKey: e.metaKey, altKey: e.altKey, shiftKey: e.shiftKey, isComposing: ime.isComposing(e) }, source !== null, p ? focusAt.current.get(p.id) ?? null : null)
    if (fate.to === 'browser') return
    e.preventDefault()
    if ((fate.to === 'key' || fate.to === 'text') && source && p) { lastKeyAt.current = performance.now(); asksRef.current?.typedInto(p.id) }
    if (fate.to === 'key') { source?.key?.({ type: 'down', key: fate.key }); return }
    if (fate.to === 'text') { source?.text?.(fate.text); return }
    const step = { ArrowLeft: [-15, 0], ArrowRight: [15, 0], ArrowUp: [0, 10], ArrowDown: [0, -10] }[e.key]
    if (!step) return
    turning.current = null
    // A step is a share of what is in view: with the eye nearer, a key turns the head by less. Stepped back, a step is as at the start.
    const by = Math.max(1, near.current)
    turn.current = { yawDeg: turn.current.yawDeg + step[0] / by, pitchDeg: Math.max(-LOOK.maxPitchDeg, Math.min(LOOK.maxPitchDeg, turn.current.pitchDeg + step[1] / by)) }
  }
  /** Back to the start picture: a level head that looks straight ahead, through the start lens, with the frame shifted down to the crew under the first page. */
  const startView = () => {
    turning.current = null
    turn.current = { ...START_TURN }
    near.current = 1
    shift.current = startShift
    // With a desk the start view looks down until the band of seats and tiles is whole over the picture's lower edge.
    const d = deskRef.current?.desk
    if (d && !bridge.current.inSession) {
      const v = bridge.current.view.width > 1 ? bridge.current.view : { width: window.innerWidth, height: window.innerHeight }
      const lowerTan = Math.tan((lensFor(v.width, v.height, 1) * Math.PI) / 360) * (1 + LOOK.startShift * shiftShare(1))
      const h = bridge.current.head
      turn.current = { ...START_TURN, pitchDeg: deskStartPitch(d, [h.x, h.y, h.z], lowerTan) }
    }
    asksRef.current?.lensChanged()
  }
  const startViewNow = useRef(startView)
  startViewNow.current = startView
  // The new chat card (`chatCard.ts`): out of the palette's "new chat", thrown to a Bot or into the open.
  const chatCard = useChatCard({
    head: () => bridge.current.head,
    lookYawDeg: () => bridge.current.lookYawDeg,
    desk: () => deskRef.current?.deskNow.current ?? null,
    quietUntil: () => (inXr() ? pinchRoute.quietUntil : quiet.current),
    dotSeenSince: t => !inXr() || pinchRoute.dotSeenSince(t),
    bots: () => crewNow.current,
    bodies: () => crewRead.current?.() ?? [],
    openPage: path => { openPage(path) },
    say,
    room: rooms.rooms.now,
  })
  const chatCardNow = useRef(chatCard)
  // No room is warmed ahead while his hands are busy or a card of his is in his hand, being typed on, or on its way.
  // A key or the wheel that went to a page in the last 4 s holds warm ahead too: he types or scrolls.
  aheadHeld.current = () => handsBusy() || handBusyNow.current || performance.now() - Math.max(lastKeyAt.current, lastWheelAt.current) < ROOM_REST_MS || ['held', 'typing', 'flying', 'sending'].includes(chatCardNow.current.card?.phase ?? '')
  chatCardNow.current = chatCard
  const deskState = useDesk(deskOn, {
    head: () => bridge.current.head,
    inSession,
    openListed: path => { openListed(path) },
    startView: () => startViewNow.current(),
    roomChanged,
    newChat: chatCard.open,
  }, crew.map(b => b.slug), statesKnown)
  deskRef.current = deskOn ? deskState : null
  // The flat preview opens on the start view, which a desk lowers: once, when the desk first stands.
  const deskStarted = useRef(false)
  useEffect(() => {
    if (!deskOn || inSession || deskStarted.current || !deskState.desk) return
    deskStarted.current = true
    startViewNow.current()
  }, [deskOn, inSession, deskState.desk])
  // At the desk: the Bots that have a seat, each at its own, and the others' pad past the last seat.
  const seating = deskState.seating
  const seatOf = useMemo(() => new Map(seating.seats.flatMap((slug, i) => (slug ? [[slug, i] as const] : []))), [seating])
  const seated = useMemo(() => (deskOn ? crew.filter(b => seatOf.has(b.slug)) : crew), [deskOn, crew, seatOf])
  const sinceCrew = useMemo(() => ({ lit: since.lit, onTap: since.tapped }), [since.lit, since.tapped])
  sinceRead.current = () => ({ ...since.probe(), line: sinceLineRead.current?.() ?? null })
  const morePad = deskOn && seating.more.length ? { count: seating.more.length, ...seatPlaces(seating.seats.length)[seating.seats.length - 1] } : null
  // The boards (`board.ts`): a Bot that waits on an approval holds one up; its page mark opens the Bot's page. Only a
  // Bot whose body is drawn holds one (at the desk, one with a seat): a Bot past the last seat asks as before.
  const drawnBots = useMemo(() => new Set(seated.map(b => b.slug)), [seated])
  const commentNow = useRef<ReturnType<typeof useComment> | null>(null)
  const sendNote = useCallback((slug: string, text: string, from?: SentFrom, id?: string) => commentNow.current?.send(slug, text, from, id), [])
  const backAtStart = useRef(placesHome.current).current
  const notesAtStart = useRef(placesNotes.current).current
  // He took a sheet in his hand: the line of what changed lets it go, and a failed Bot's cut is seen now, not at the pinch.
  const sinceLooked = since.looked
  const lookedSheet = useCallback((slug: string, took: 'done' | 'failed') => { sinceLooked(slug, took); if (took === 'failed') pressRim(slug) }, [sinceLooked, pressRim])
  // A Bot he pinched for its words may hold its sheet though its body is not drawn: the sheet comes to his hand (D92 1).
  const saysPinned = says.pinned
  const boardsDrawn = useMemo(() => (saysPinned.size ? new Set([...drawnBots, ...saysPinned]) : drawnBots), [drawnBots, saysPinned])
  const boards = useBoards(crew, { say, openBot, known: statesKnown, drawn: boardsDrawn, words: says.says, read: says.read, keep: says.keep, looked: lookedSheet, sendNote, backAtStart, notesAtStart, micAway: chatCard.micAway })
  // His notes on boards go to the room's record as they change, so a reload keeps them (M45).
  useEffect(() => {
    if (JSON.stringify(boards.keptNotes) === JSON.stringify(placesNotes.current)) return
    placesNotes.current = boards.keptNotes
    placedSoon.current()
  }, [boards.keptNotes, placesNotes])
  cardMicAway.current = chatCard.micAway
  boardViewsNow.current = boards.views
  // His note on the board in his hand (`useComment.ts`): the line a pinch on the paper's foot opened.
  // Or the sheet a Bot holds up while he speaks to it (P2), first: one listener at a time, and the hold's words go to it.
  const writingOn = boards.views.find(v => v.writing && v.kind === 'speak') ?? boards.views.find(v => v.writing && v.where === 'held')
  // The room's name line is written as a note is (D93 2): the same keys and the same microphone. A board he writes
  // on goes first. Its id is the room's with "room:" before it.
  const nameAsk = rooms.nameAsk
  const roomTyped = rooms.typeName
  const roomEnter = rooms.keepName
  const boardNote = boards.note
  const boardFoot = boards.foot
  const writeNote = useCallback((id: string, text: string) => (id.startsWith('room:') ? roomTyped(id.slice(5), text) : boardNote(id, text)), [roomTyped, boardNote])
  const closeNote = useCallback((id: string) => (id.startsWith('room:') ? roomEnter(id.slice(5)) : boardFoot(id)), [roomEnter, boardFoot])
  // A new room's asking ends when he opens a page in it: the room keeps "Room n".
  const roomEndAsk = rooms.endAsk
  // (The old room's pages still stand for a moment after "New room": only a page that comes once they are gone counts.)
  const roomEmptied = useRef('')
  const openHomeNow = useRef<() => void>(() => undefined)
  useEffect(() => {
    if (!nameAsk?.fresh) return
    if (panels.length === 0) { if (roomEmptied.current !== nameAsk.id) { roomEmptied.current = nameAsk.id; openHomeNow.current() } }
    else if (roomEmptied.current === nameAsk.id) roomEndAsk(nameAsk.id)
  }, [nameAsk, panels.length, roomEndAsk])
  const comment = useComment(writingOn ? { id: writingOn.needId, comment: writingOn.comment, keysAlways: writingOn.kind !== 'speak' } : nameAsk ? { id: `room:${nameAsk.id}`, comment: nameAsk.text, keysAlways: true } : null, writeNote, closeNote, { head: () => bridge.current.head, lookYawDeg: () => bridge.current.lookYawDeg, bots: () => crewNow.current, say })
  // What the keys write, shown over their far row (D97 1): a note on the board he writes on, or the room's name with why it was refused.
  const keysWrite = useMemo(() => (writingOn ? { text: writingOn.comment, interim: comment.hearing.interim } : nameAsk ? { text: nameAsk.text, interim: comment.hearing.interim, said: rooms.said } : null), [writingOn, nameAsk, comment.hearing.interim, rooms.said])
  commentNow.current = comment
  /** A hold on a Bot (P2, `speak.ts`): its sheet comes up; his release sends the words it shows, what is still heard too. */
  const holdBoards = { start: boards.holdStart, end: boards.holdEnd }
  const holdNow = useRef(holdBoards)
  holdNow.current = holdBoards
  const holdBot = useCallback((slug: string, phase: 'start' | 'drag' | 'let' | 'cancel'): boolean => {
    if (phase === 'start') { holdNow.current.start(slug); return false }
    return holdNow.current.end(slug, phase, commentNow.current?.read().interim ?? '')
  }, [])
  /** The asks whose boards the Bots hold up now: a 3D dashboard that is taken goes back when one comes up. */
  const boardsUp = useMemo(() => new Set(boards.views.filter(b => b.where === 'bot').map(b => b.needId)), [boards.views])
  boardCrewNow.current = seated
  boardAsksNow.current = boards.asks
  boardHoldersNow.current = boards.holders
  boardsAnsweredNow.current = boards.answered
  handedBackNow.current = boards.backBots
  // A board in his hand stands in front of the card's keyboard: the keyboard is put away until it leaves his hand.
  const boardInHand = boards.views.some(b => b.where === 'held')
  handBusyNow.current = boardInHand || boards.views.some(v => v.kind === 'speak')
  const keysAway = chatCard.keysAway
  useEffect(() => { keysAway(boardInHand) }, [boardInHand, keysAway])
  // With no row (`places.ts`): what the crew reads of the room, and Home as it is shown there (`usePlacesRoom.ts`).
  const { placesRoom, homeShown, pressHome, openHome, homeOpens, setTrayTaken } = usePlacesRoom({
    placesOn, panels, ahead: roomAhead, home, crew, statesKnown, placesKept, placesPut, setPlacesKept, setPlacesPut, placesPutNow, crewNow, placedSoon, bridge, placesFront, placesHome,
    backAsks: boards.backAsks, cardKeys: chatCard.keys, handBusy: boardInHand || boards.views.some(v => v.kind === 'speak'),
  })
  openHomeNow.current = openHome
  const boardsRead = useRef(boards.probe)
  boardsRead.current = boards.probe
  const boardsStale = useRef(boards.stale)
  boardsStale.current = boards.stale
  const boardMarksRead = useRef<(() => Record<string, BoardMarks>) | null>(null)
  const reportBoards = useCallback((read: (() => Record<string, BoardMarks>) | null) => { boardMarksRead.current = read }, [])
  // A Bot's sign counts its asks only while no board of it is in view: the asks behind a board are counted on its clip (D24).
  const platesShown = useMemo(() => new Map([...(asks.plates ?? new Map<string, number>())].flatMap(([slug, n]) => { const left = boards.held.get(slug) ? 0 : n; return left > 0 ? [[slug, left] as const] : [] })), [asks.plates, boards.held])
  // A press on a line of what waits on the Home surface, and its wait for a board (`useHomeLines.ts`).
  const { lineShows, takeWaiting, touchDown } = useHomeLines({ lines: home.lines, boards, crew, crewNow, drawnBots, inSession, anyDownAt, openBot, openListed })
  // Who needs him, by name, on Home's strip; a pinch on a name takes that Bot's board as at its desk (`useHomeNames.ts`).
  const homeNamed = useHomeNames({ crew, known: statesKnown, inSession, boards, drawnBots, tap: tapBot, openBot, anyDownAt })
  // A pinch on a Bot at its desk, in a session, brings what it has into his hand (D77, `useBotSheet.ts`).
  const bodyOf = useCallback((slug: string) => crewRead.current?.().find(p => p.slug === slug), [])
  const botSheet = useBotSheet({ crew, views: boards.views, inSession, body: bodyOf, take: boards.take, tap: tapBot, openBot, say: sayAtBot, anyDownAt })
  botSheetNow.current = botSheet.last
  // The hands read the desk's height for who is raised and which pinch counts (`pinchRoute.ts`); with no word, none.
  useEffect(() => {
    pinchRoute.deskY = deskOn && deskState.desk ? deskState.desk.at[1] : null
    return () => { pinchRoute.deskY = null }
  }, [deskOn, deskState.desk])

  /**
   * Two fingers anywhere on the canvas turn the head and bring the eye nearer, also where pages fill the view and
   * leave no empty space. The room follows the point halfway between them; apart, they bring the eye nearer to what
   * is under that point, and together they take it back. From the moment the second finger lands until every
   * finger is lifted the canvas hears nothing more of them, and what the first finger had begun (a press on a page,
   * a drag of empty space) is called off as the browser calls a touch off: a turn or a pinch never ends as a press.
   */
  const middle = (canvas: HTMLCanvasElement): [number, number] => {
    const box = canvas.getBoundingClientRect()
    let x = 0
    let y = 0
    for (const [fx, fy] of fingers.current.values()) { x += fx; y += fy }
    return [x / fingers.current.size - box.left, y / fingers.current.size - box.top]
  }
  const spread = (): number => {
    const [a, b] = [...fingers.current.values()]
    return Math.hypot(a[0] - b[0], a[1] - b[1])
  }
  const twoFingers = {
    onPointerDownCapture: (e: ReactPointerEvent<HTMLDivElement>) => {
      // The call-off below is an event of this page's own making. Only the browser's own count as fingers.
      if (!e.isTrusted) return
      downs.current.set(e.pointerId, performance.now())
      anyDownAt.current = performance.now()
      const box = e.currentTarget.querySelector('canvas')?.getBoundingClientRect()
      if (box && heldBack()) {
        // The touch is held back: the canvas never hears of it. A tap does nothing, and the turn goes on to its
        // page. Only when it moves is it a drag (below).
        quiet.current = quietAfter(quiet.current, performance.now(), afterClose.current)
        if (!taken.current || e.isPrimary) taken.current = { pointerId: e.pointerId, at: [e.clientX - box.left, e.clientY - box.top], grab: null }
        e.stopPropagation()
      } else {
        // A new touch that is a press: whatever was missed of an earlier one, the room is in no hand.
        if (e.isPrimary) taken.current = null
        // A mouse's press lands where the pointer is, whatever moves of it were held back before. (A finger's
        // touch is a new pointer each time, and comes onto the canvas at its own place.)
        if (e.pointerType !== 'touch' && !inXr()) tellCanvas(e.pointerId, e.pointerType, e.clientX, e.clientY)
        else known.current.add(e.pointerId)
      }
      if (e.pointerType !== 'touch') return
      // The first finger of a touch: none other is down, whatever was missed before.
      if (e.isPrimary) { fingers.current.clear(); two.current = null }
      fingers.current.set(e.pointerId, [e.clientX, e.clientY])
      const canvas = e.currentTarget.querySelector('canvas')
      if (fingers.current.size === 2 && canvas) {
        if (!two.current) for (const id of fingers.current.keys()) if (id !== e.pointerId) canvas.dispatchEvent(new PointerEvent('pointercancel', { pointerId: id, pointerType: 'touch', bubbles: true }))
        turning.current = null
        two.current = { grab: under(turn.current, ...middle(canvas), bridge.current.view), spread: spread(), near: near.current, shift: bridge.current.view.shift ?? 0 }
      }
      if (two.current) e.stopPropagation()
    },
    onPointerMoveCapture: (e: ReactPointerEvent<HTMLDivElement>) => {
      if (!e.isTrusted) return
      const own = taken.current
      // A mouse whose button came up where this did not hear it holds nothing any more.
      if (own?.pointerId === e.pointerId && e.pointerType !== 'touch' && e.buttons === 0) taken.current = null
      if (taken.current?.pointerId === e.pointerId && own) {
        e.stopPropagation()
        const box = e.currentTarget.querySelector('canvas')?.getBoundingClientRect()
        // With a second finger down the two of them turn the head, below.
        if (box && !two.current) {
          const x = e.clientX - box.left
          const y = e.clientY - box.top
          // A drag ends the turn, and the picture follows the finger from where it is: what is under the place
          // of the press now comes along with the finger. (Taken there, and not under the finger: a browser may
          // hand over a quick drag as one move.)
          if (!own.grab && Math.hypot(x - own.at[0], y - own.at[1]) >= TURN_DRAG_PX) { turning.current = null; own.grab = under(turn.current, own.at[0], own.at[1], bridge.current.view) }
          if (own.grab) turn.current = hold(own.grab, x, y, bridge.current.view, turn.current)
        }
        if (e.pointerType !== 'touch') missed.current = { pointerId: e.pointerId, pointerType: e.pointerType, x: e.clientX, y: e.clientY }
      } else if (e.buttons === 0 && heldBack()) {
        // A pointer that only passes over the canvas in the quiet time: no page hears of it, and nothing lights.
        missed.current = { pointerId: e.pointerId, pointerType: e.pointerType, x: e.clientX, y: e.clientY }
        e.stopPropagation()
      } else known.current.add(e.pointerId)
      if (!fingers.current.has(e.pointerId)) return
      fingers.current.set(e.pointerId, [e.clientX, e.clientY])
      const held = two.current
      if (!held) return
      e.stopPropagation()
      const canvas = e.currentTarget.querySelector('canvas')
      // With one of the two lifted the head rests: the finger that is left neither turns it nor presses.
      if (fingers.current.size !== 2 || !canvas) return
      const view = bridge.current.view
      turning.current = null
      near.current = pinchNear(held.near, held.spread, spread(), mostNear(view.width, view.height), leastNear(view.width, view.height))
      asksRef.current?.lensChanged()
      // In the frame that the fingers took hold in: the frame's own shift may have changed with the eye's nearness since.
      turn.current = hold(held.grab, ...middle(canvas), viewAt({ ...view, shift: held.shift }, near.current), turn.current)
    },
    // The pointer comes onto the canvas, or its wheel turns, in the quiet time: neither reaches a page or a panel.
    onPointerOverCapture: (e: ReactPointerEvent<HTMLDivElement>) => {
      if (!e.isTrusted) return
      if (heldBack()) e.stopPropagation()
      else known.current.add(e.pointerId)
    },
    onWheelCapture: (e: ReactWheelEvent<HTMLDivElement>) => { if (e.isTrusted && heldBack()) e.stopPropagation() },
    onPointerUpCapture: (e: ReactPointerEvent<HTMLDivElement>) => liftFinger(e),
    onPointerCancelCapture: (e: ReactPointerEvent<HTMLDivElement>) => liftFinger(e),
  }
  function liftFinger(e: ReactPointerEvent<HTMLDivElement>) {
    if (!e.isTrusted) return
    downs.current.delete(e.pointerId)
    if (taken.current?.pointerId === e.pointerId) {
      taken.current = null
      e.stopPropagation()
      // A mouse stays over the canvas after its press: the canvas is told where, now or when the quiet time is up.
      if (e.pointerType !== 'touch' && e.type === 'pointerup') {
        if (hushedNow.current) missed.current = { pointerId: e.pointerId, pointerType: e.pointerType, x: e.clientX, y: e.clientY }
        else tellCanvas(e.pointerId, e.pointerType, e.clientX, e.clientY)
      }
    }
    // The end of a touch that the canvas never heard of is not for it either (see the leave, above).
    else if (!known.current.has(e.pointerId)) e.stopPropagation()
    if (!fingers.current.delete(e.pointerId) || !two.current) return
    e.stopPropagation()
    if (fingers.current.size === 0) two.current = null
  }

  return (
    <div className="flex h-full min-h-0 w-full flex-col" data-testid="xr-pages">
      {/* On a phone held on its side the bar is thinner: with the dashboard's own bar it stays under a fifth of the height. */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border bg-bg-elevated px-3 py-2 [@media(max-height:480px)]:py-0.5">
        <Btn onClick={() => navigate('/chat')} aria-label={i18nT('xr.back')} title={i18nT('xr.back')}>
          <ArrowLeft size={15} />
        </Btn>
        <h1 className="m-0 text-[15px] font-semibold text-text">{i18nT('xr.title')}</h1>
        {fixture && <span className="inline-flex items-center gap-1 rounded-full border border-border bg-bg-elevated px-2 py-0.5 text-[12px] text-muted" data-testid="xr-sample">{i18nT('xr.sample_data')}</span>}
        <RoomsBar {...roomCards} onRename={rooms.rename} onRemove={rooms.remove} />
        <span className="flex-1" />
        <Btn onClick={() => { chatCard.open(); host.current?.querySelector<HTMLElement>('[data-testid="xr-canvas"]')?.focus() }} aria-label={i18nT('pages.chat.sessionFlyout.new_chat')} title={i18nT('pages.chat.sessionFlyout.new_chat')} data-testid="xr-new-chat">
          <MessageSquarePlus size={15} />
        </Btn>
        <OpenPage onOpen={openListed} />
        <Btn onClick={() => setWalls(on => !on)} aria-pressed={walls} aria-label={i18nT(walls ? 'xr.walls_hide' : 'xr.walls_show')} title={i18nT(walls ? 'xr.walls_hide' : 'xr.walls_show')} data-testid="xr-walls">
          <Box size={15} />
        </Btn>
        <Btn onClick={home.toggle} aria-pressed={home.pagesHidden} aria-label={i18nT(home.pagesHidden ? 'xr.home_pages_show' : 'xr.home_pages_hide')} title={i18nT(home.pagesHidden ? 'xr.home_pages_show' : 'xr.home_pages_hide')} data-testid="xr-pages-switch">
          <PanelsTopLeft size={15} />
        </Btn>
        <Btn onClick={startView} aria-label={i18nT('xr.start_view')} title={i18nT('xr.start_view')} data-testid="xr-start-view">
          <Home size={15} />
        </Btn>
        {support !== 'none' && (
          <Btn primary onClick={() => void enter()} data-testid="xr-enter">
            <RectangleGoggles className="lucide-inline" /> {i18nT('xr.enter')}
          </Btn>
        )}
      </div>
      {failure && <ErrorNotice title={i18nT('xr.load_failed')} message={failure} onDismiss={() => setFailure('')} askAgent variant="inline" />}
      <div className="relative flex min-h-0 flex-1 flex-col">
        {/* `touch-none`: a finger on the canvas is the canvas's own. The dashboard round it does not scroll, zoom or bounce. */}
        <div
          className="relative min-h-0 flex-1 touch-none select-none overflow-hidden"
          data-testid="xr-host"
          ref={host}
          {...twoFingers}
        >
          <Canvas
            className="block h-full w-full outline-none"
            tabIndex={0}
            aria-label={i18nT('xr.pages_label')}
            data-testid="xr-canvas"
            onKeyDown={onKeyDown}
            flat
            dpr={[1, 2]}
            gl={GL}
            camera={CAMERA}
            // The framework's own pointer events, for the mouse as for the hands.
            events={noEvents}
          >
            <PinchOpen.Provider value={pinchOpenMs}>
              <XR store={store}>
                <Mouse />
                <HeadAim />
                {fps && inSession && <ReadoutPlate pages={readoutPages} frames={readoutFrames} draws={readoutDraws} canvas={readoutCanvas} notes={readoutAll} said={onPlateLines} probe={probe ? onPlateCovers : undefined} />}
                <EmptySpace bridge={bridge} turn={turn} turning={turning} near={near} shift={shift} zoomAt={zoomAt} zoomer={zoomer} noDesk={deskOn} onPress={onEmptyPress} onLens={onLens} walls={walls}>
                  {hand => <>
                    {crewColors && crew.length > 0 && <Crew desk={deskOn ? deskState.desk : null} seatOf={deskOn ? seatOf : undefined} spots={deskOn ? seating.seats.length : undefined} bots={seated} head={bridge.current.head} colors={crewColors} still={still} hushed={hushed && !inSession} known={statesKnown} onTap={botSheet.press} drag={hand} report={reportCrew} plates={platesShown} boarded={boards.holders} kept={deskOn ? undefined : crewKept} onPlace={placeBot} onSeat={deskOn ? deskState.moveSeat : undefined} room={placesRoom} onHold={holdBot} since={sinceCrew} notes={botNotes} />}
                    <HomeSurface home={homeShown} hushed={hushed && !inSession} does={lineShows} onTake={takeWaiting} onDone={pressDone} names={homeNamed.names} onName={homeNamed.press} nameDoes={homeNamed.does} onNamesOver={homeNamed.over} drag={hand} report={reportHome} onPlate={placesOn ? pressHome : undefined} opens={homeOpens} above={placesOn} />
                    <HomeSince home={homeShown} above={placesOn} line={since.line} hushed={hushed && !inSession} now={sinceClock} onClear={since.clear} report={reportSince} />
                    {crewColors && dash.views.length > 0 && <Dash3d views={dash.views} crew={() => crewRead.current?.() ?? []} block={trayBlock(boardInHand, chatCard.keys !== null)} boardsUp={boardsUp} say={say} drag={inSession ? undefined : hand} still={still} stateColors={crewColors.state} report={probe ? reportDash : undefined} onTaken={setTrayTaken} />}
                  </>}
                </EmptySpace>
                {/* Mounted with no board too: the last board's leaving is drawn after it is gone from the list. */}
                {crewColors && <Boards boards={boards.views} crew={() => crewRead.current?.() ?? []} head={bridge.current.head} inSession={inSession} still={still} onTake={boards.take} onStamp={boards.stamp} onRelease={boards.release} onPick={boards.pick} onCorner={boards.corner} onPage={boards.page} onStampBox={boards.stampBox} onFoot={boards.foot} hearing={comment.hearing} onAway={boards.away} onHidden={boards.hide} band={homeBand} report={probe ? reportBoards : undefined} />}
                <Room bridge={bridge} turn={turn} turning={turning} near={near} shift={shift} />
                {panels.filter(p => !home.hides(p.id)).map(p => <Panel key={p.id} id={p.id} source={p.source} position={p.position} turned={p.turned} quality={quality} head={bridge.current.head} using={bridge.current.using} report={report} onPlaced={onPlaced} layers={layerShare} onNote={onNote} onPress={onPress} mayPress={mayPress} pressHeld={pressHeld} mayWheel={mayWheel} wheelLatched={zoomLatched} onWheelOn={onWheelOn} onFocus={onFocus} onClose={closePage} quiet={quiet} hushed={hushed} walled={walls} markAtRest={front === p.id} flash={flashed === p.id} glideMs={p.glide ? ROOM_GLIDE_MS : 0} glideBehindM={p.behindM} seam={seamOf(p.id)} />)}
                {asks.signs.map(sign => <AskSign key={`${sign.id}:${sign.what}`} sign={sign} probe={probeOf} />)}
                {deskOn && inSession && <DeskTouch report={probe ? reportTouch : undefined} onDown={touchDown} />}
                {deskOn && deskState.desk && <DeskRoomCards desk={deskState.desk} {...roomCards} />}
                {deskOn && <DeskSetup armedAt={deskState.armedAt} onDone={deskState.setupDone} report={probe ? reportSetup : undefined} />}
                {deskOn && deskState.desk && <Desk desk={deskState.desk} tiles={deskState.tiles} rows={deskState.rows} onTile={id => { deskState.confirm(); deskState.onTile(id) }} onRow={path => { deskState.confirm(); deskState.onRow(path) }} more={morePad} hushed={hushed && !inSession} solid={walls} report={probe ? reportDesk : undefined} />}
                {!deskOn && <Palette store={store} head={bridge.current.head} icons={menuIcons} onOpen={openPage} onNewChat={chatCard.open} report={probe ? reportPalette : undefined} rooms={roomCards} two={inSession && (twoOn || panels.filter(p => !asksRef.current?.isAsk(p.id)).length >= 2) ? { on: twoOn, onPress: twoToggle } : undefined} />}
                <ChatCard state={chatCard} head={bridge.current.head} labelOf={slug => crewNow.current.find(b => b.slug === slug)?.label ?? slug} hushed={hushed && !inSession} />
                {nameAsk && !writingOn && inSession && homeShown.form === 'large' && <RoomNameLine head={bridge.current.head} yawDeg={nameAsk.yawDeg} name={rooms.rooms.rooms.find(k => k.id === nameAsk.id)?.name ?? ''} text={nameAsk.text} interim={comment.hearing.interim} said={rooms.said} />}
                {comment.keys && <DeskKeys place={comment.keys} head={bridge.current.head} onKey={comment.key} hushed={hushed && !inSession} line={keysWrite} drawn={comment.drawn} />}
                {/* After the panels, so that it runs after them in a frame. */}
                <Layers store={store} share={layerShare} head={bridge} off={layersOff} />
                <StreamWatch blocked={boardInHand || boards.views.some(v => v.kind === 'speak') || home.pagesHidden} />
                {fps && <DeviceRead share={layerShare} layersOff={layersOff} />}
                {speechOn && <SpeechTiles />}
                {fps && !inSession && <PageRects into={pageRects} />}
                {sendTo && <SendNumbersTile to={sendTo} pages={readoutPages} frames={readoutFrames} draws={readoutDraws} canvas={readoutCanvas} notes={readoutAll} />}
              </XR>
            </PinchOpen.Provider>
          </Canvas>
          {line && (
            <p role="status" data-testid="xr-note" className={`pointer-events-none absolute inset-x-2 ${lineTop ? 'top-2' : field ? 'bottom-16' : 'bottom-2'} m-0 mx-auto w-fit max-w-full rounded-lg border border-border bg-bg-elevated px-3 py-1.5 text-center text-[12px] text-text`}>
              {line}
            </p>
          )}
        </div>
        {/* Outside the box that counts fingers: a finger on the readout is no finger on the canvas. */}
        {fps && !inSession && <Readout pages={readoutPages} frames={readoutFrames} draws={readoutDraws} canvas={readoutCanvas} notes={readoutAll} avoid={readoutAvoid} />}
        {!inSession && <HomeMirror home={home} does={lineShows} onTake={takeWaiting} onDone={pressDone} />}
        {!inSession && <HomeSinceMirror line={since.line} now={sinceClock} onClear={since.clear} />}
        {/* The canvas is a picture. This list gives the same Bots to a screen reader and to Tab. */}
        {!inSession && crew.length > 0 && (
          <ul className="sr-only" aria-label={i18nT('xr.title')} data-testid="xr-crew-list">
            {crew.map(b => (
              <li key={b.slug}>
                {/* While a Bot's state cannot be known the list names it and says no state, as the picture does. */}
                <Btn onClick={() => openBot(b.slug)}>{statesKnown ? `${b.label}: ${b.needs.length ? i18nT('xr.needs_you', { count: b.needs.length }) : b.line}` : b.label}</Btn>
              </li>
            ))}
          </ul>
        )}
        {inSession && (
          <div ref={menuIcons} className="hidden" aria-hidden="true">
            {PAGE_MENU.map((page, i) => <span key={page.path} data-menu-icon={i}>{page.icon}</span>)}
          </div>
        )}
        {/* Outside the canvas's own box: a finger on the field is not one of the canvas's fingers. */}
        {field && !inSession && (
          <PhoneField
            key={`${field}:${secret ? 'secret' : 'text'}`}
            secret={secret}
            onText={text => { const to = keysFor(field); if (to) { lastKeyAt.current = performance.now(); asksRef.current?.typedInto(field) } to?.text?.(text) }}
            onKey={key => { const to = keysFor(field); if (to) { lastKeyAt.current = performance.now(); asksRef.current?.typedInto(field) } to?.key?.({ type: 'down', key }) }}
            onDone={() => { putAway.current = true; settleField() }}
            held={() => keysFor(field)?.notYet?.() ?? false}
          />
        )}
      </div>
    </div>
  )
}
