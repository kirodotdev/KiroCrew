/**
 * The shapes the XR scene works with: what the page hands it, and what it
 * keeps while a hand holds something.
 */
import type { Object3D, PerspectiveCamera, Ray, Vector3 } from 'three'
import type { XrSourceLike } from './sources'
import type { RaySample } from './handling'
import type { PlotMap } from './land'
import type { PageRec } from './pages'
import type { Hit } from './painter'
import type { XrPalette } from './palette'
import type { Surface } from './sceneParts'
import type { XrStrings } from './strings'
import type { TwoHandRule } from './tune'

export interface XrWant { threads: string[]; pages: number[]; bots: string[] }

/** The dashboard's own speech recogniser, started and stopped by the scene. Words come back through `heard`. */
export interface XrSpeech { start: () => void; stop: () => void }

export interface XrSceneOptions {
  /** The box the framework's canvas fills. It takes the pointer and the wheel, and carries the test probe. */
  host: HTMLElement
  /** The camera of the desktop preview. The framework makes it and keeps its aspect; the scene moves it. */
  camera: PerspectiveCamera
  strings: XrStrings
  palette: XrPalette
  icons: ReadonlyMap<string, CanvasImageSource>
  /** Sample data: a fixed clock, a scripted voice stand-in and a test probe. */
  fixture: boolean
  /** Write the test probe even on live data (the dev-gateway tests need it). */
  probe: boolean
  /** Which scene document to draw (`sceneDoc.ts`). */
  scene: string
  /** How two hands on one thing are measured (`tune.ts`). */
  twoHand: TwoHandRule
  /** Speech is on. Off, which is how the page opens, the person uses hands only: no Bot listens and no word answers. */
  voice: boolean
  /** `aura` frames the desktop preview to what the glasses show, and holds the head still. */
  view: string
  /** With a framed view: how many degrees below level the head looks. */
  look: number
  reducedMotion: boolean
  /** Wall clock in ms, for "read 3 minutes ago". */
  now: () => number
  /** What the fixture's voice stand-in "hears", and what its scripted Bot answers. */
  voiceLine: string
  voiceReply: string
  /** Each Bot's plot, and the pages laid out, from the last visit. */
  plots: PlotMap
  pages: readonly PageRec[]
  speech: XrSpeech | null
  onWant: (want: XrWant) => void
  /** Open a dashboard page. In a session this ends XR first. */
  onLeave: (to: string) => void
  onSay: (text: string) => void
  onHand: (shown: boolean) => void
  /** After a write went through: the page re-reads what it changed. */
  onWrote: () => void
  onPlots: (plots: PlotMap) => void
  onPages: (pages: PageRec[]) => void
}

export interface Page {
  s: Surface
  rec: PageRec
  scale: number
  toast: string
  toastUntil: number
  /** How many pages of earlier chat were asked for. */
  earlier: number
  /** Until this time the page glides to its place as a thrown thing does, slower than a placed one. */
  glideUntil: number
}

export interface Pick {
  id: string
  bot?: string
  obj?: string
  ground?: { seat: Vector3; x: number; z: number }
  surface?: Surface
  /** A token of the interaction layer: `card` (the request a Bot holds out), `marble:<id>` or `bin`. */
  tok?: string
  hit?: Hit | null
  px?: [number, number]
  point: Vector3
}

export interface Drag {
  /** What is being dragged: a page by its header or corner, a row of a page, the hand's grip, a pull out of a Bot or a thing of the land, a thing in the air, or the card a Bot holds out. */
  kind: NonNullable<Hit['drag']> | 'pull' | 'carry' | 'card'
  source: string
  dir: Vector3
  at: number
  active: boolean
  page?: Page
  startScale?: number
  startDist?: number
  /** For a row: the Bot and the field it shows. */
  row?: { bot: string; field: string }
  /** For a pull or a carried thing: what the pinch began on, and its own title. */
  pickId?: string
  label?: string
  /** How the hand has turned over the last moment: what a throw is read from. */
  samples: RaySample[]
  /** Where a carried page or thing was before the hand took it: it goes back there when it is given to a Bot. */
  home?: { yaw: number; pitch: number }
  homePos?: [number, number, number]
  /** What a Bot would be given if this were let go on it: the id of the work it stands for, or '' when it is not work. */
  gift?: string
  /** The hand has carried it clear of every Bot once. Only then can a Bot light for it: a thing is not given to the Bot it was just taken from. */
  clear?: boolean
  /** The page came out of its thing for this hold. Given to a Bot, it goes back in: it is not left open. */
  fresh?: boolean
}

/** One thing that can press in a session: a hand, or a device. The framework reports it; the page fills in where it is each frame. */
export interface Pointer {
  id: string
  /** The hand or device of the session it is, as the browser gave it. */
  source: XrSourceLike
  /** Where it aims from, and which way: the ray of its pointer, as the framework's pointer events give it. */
  ray: Ray
  /** A hand's joints by their WebXR names. A joint that is not tracked this frame is not `visible`. Empty for a device. */
  joints: Readonly<Record<string, Object3D>>
  handed: string
  /** When its pinch opened, while that is not yet counted as a release (0 otherwise), and what to do once it is. */
  openAt: number
  letGo: () => void
}
